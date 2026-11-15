"""Launch one training subprocess and persist one evolving incident per launch.

No shell invocation, API keys, phone calls, automatic patches, or restarts.
Call watch(..., on_incident=...) to connect your team's incident/triage module.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from .detectors import Plateau, log_failure, non_finite
from .metrics import MetricsTail



def clean(value):
    """Normalize non-standard NaN/Infinity to strict JSON before persistence."""
    if isinstance(value, float) and not math.isfinite(value):
        return None

    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
   
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
   
    return value



def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(clean(value), indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)



def watch(command, run_dir, cwd=None, config=None, poll=0.1, plateau_steps=0, min_delta=0.001, on_incident=None):
    """Return child's exit code; on_incident receives immutable JSON snapshots.

    A callback is invoked on first detection and severity/type upgrades only.
    Persist before delivery; callback failures do not erase the incident.
    The consumer must deduplicate on incident id and handle LOW -> HIGH upgrades.
    """
    if not command or poll <= 0 or not math.isfinite(poll):
        raise ValueError('A command and positive finite poll interval are required')
    
    if plateau_steps < 0 or min_delta < 0 or not math.isfinite(min_delta):
        raise ValueError('Invalid plateau settings')
    
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    incident_dir = run_dir / 'incidents'
    incident_dir.mkdir(exist_ok=True)
    session = uuid.uuid4().hex[:12]
    # Ignore historical rows when resuming into an existing run directory.
    tail = MetricsTail(run_dir / 'metrics.jsonl', start_at_end=True)
    plateau = Plateau(plateau_steps, min_delta)
    logs, metrics = deque(maxlen=200), deque(maxlen=200)
    incident = None
    priority = {'plateau': 0, 'crash': 1, 'nan': 2, 'oom': 3}
    events = queue.Queue()
    process = None
    interrupted = False
    lock_path = run_dir / '.watcher.lock'
    # Exclusive creation prevents two watcher launches writing the same run.
    try:
        lock = lock_path.open('x', encoding='utf-8')
    except FileExistsError:
        raise RuntimeError(f'Watcher lock exists: {lock_path}. Check for a running watcher before removing a stale lock.')
    
    with lock:
        lock.write(str(os.getpid()))
    try:
        with (run_dir / f'watcher-{session}.log').open('a', encoding='utf-8', buffering=1) as output:
            def log(message):
                print(message, flush=True)
                output.write(message + '\n')

            def persist():
                if incident is None:
                    return
                incident['log_tail'] = list(logs)
                incident['metrics_tail'] = list(metrics)
                incident['step'] = metrics[-1].get('step') if metrics else None
                trace = list(logs)
                start = next((i for i, line in enumerate(trace) if 'Traceback (most recent call last)' in line), None)
                incident['traceback'] = '\n'.join(trace[start:]) if start is not None else None
                incident['updated_at'] = datetime.now(timezone.utc).isoformat()
                atomic_json(incident_dir / f'{incident["id"]}.json', incident)

            def detect(kind, evidence):
                nonlocal incident
                changed = incident is None or priority[kind] > priority[incident['type']]
                if incident is None:
                    incident = dict(id=f'inc_{session}', run_id=run_dir.name,
                                    type=kind, detected_at=datetime.now(timezone.utc).isoformat(),
                                    config=config or {}, git_diff=None, triage=None,
                                    status='open', watcher_session=session, command=command,
                                    severity='LOW' if kind == 'plateau' else 'HIGH', evidence=[])
                if changed:
                    incident['type'] = kind
                    incident['severity'] = 'LOW' if kind == 'plateau' else 'HIGH'
                if evidence not in incident['evidence']:
                    incident['evidence'].append(evidence)
                persist()
                if changed:
                    log(f'[watcher] INCIDENT {incident["id"]} {incident["type"]} severity={incident["severity"]}')
                    if on_incident:
                        try:
                            on_incident(json.loads(json.dumps(clean(incident))))
                        except Exception as exc:
                            log(f'[watcher] callback failed: {exc}; incident remains saved')

            def read_metrics():
                for row in tail.read():
                    metrics.append(row)
                    if non_finite(row):
                        detect('nan', 'Non-finite metric')
                    elif plateau.update(row):
                        detect('plateau', 'No sufficient loss improvement within configured step window')
                for error in tail.errors:
                    log(f'[watcher] Skipped malformed metric: {error}')
                persist()

            def pump(stream, name):
                try:
                    for line in stream:
                        events.put((name, line.rstrip('\r\n')))
                finally:
                    stream.close()
                    events.put((name, None))

            def consume(name, line):
                if line is None:
                    return
                annotated = f'[{name}] {line}'
                logs.append(annotated)
                log(annotated)
                kind = log_failure(line)
                if kind:
                    detect(kind, f'{name} reports {kind}')

            try:
                process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                           errors='replace', bufsize=1, shell=False)
            except OSError as exc:
                logs.append(str(exc))
                detect('crash', 'Unable to launch training subprocess')
                return 1
            threads = []
            for name, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                thread = threading.Thread(target=pump, args=(stream, name), daemon=True)
                thread.start()
                threads.append(thread)
            log(f'[watcher] Started pid={process.pid}; metrics={tail.path}')
            try:
                # Separate pipe readers avoid deadlocks when stderr is busy.
                closed = set()
                while len(closed) < 2 or process.poll() is None:
                    try:
                        name, line = events.get(timeout=poll)
                        if line is None:
                            closed.add(name)
                        else:
                            consume(name, line)
                    except queue.Empty:
                        pass
                    read_metrics()
            except KeyboardInterrupt:
                interrupted = True
                log('[watcher] Interrupted; stopping the owned training process')
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for thread in threads:
                thread.join(timeout=2)
            while not events.empty():
                consume(*events.get_nowait())
            read_metrics()  # Catch metrics flushed just before process exit.
            code = process.wait()
            if code != 0 and not interrupted:
                detect('crash', f'Training exited with code {code}')
            if incident:
                incident['process_exit_code'] = code
                persist()
            atomic_json(run_dir / f'watcher-result-{session}.json', dict(
                exit_code=code, interrupted=interrupted,
                incident_id=incident['id'] if incident else None))
            log(f'[watcher] Finished exit_code={code}; incident={incident["id"] if incident else "none"}')
            return 130 if interrupted else code
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        lock_path.unlink(missing_ok=True)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path, help='Training YAML snapshot for incident context')
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--poll', type=float, default=0.1)
    parser.add_argument('--plateau-steps', type=int, default=0, help='0 disables plateau detection')
    parser.add_argument('--min-delta', type=float, default=0.001, help='Absolute loss improvement threshold')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='-- python -u demo/train.py ...')
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    config = None
    
    if args.config:
        import yaml
        config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            parser.error('--config must contain a YAML mapping')
    
    if not command:
        parser.error('Provide a training command after --')
    
    return watch(command, args.run_dir, args.cwd, config, args.poll, args.plateau_steps, args.min_delta)



if __name__ == '__main__':
    sys.exit(main())
