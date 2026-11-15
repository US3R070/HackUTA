"""Launch training subprocess, tail output + metrics, fire detections. Owner: Binoy."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from watcher.detectors import Detection, Detectors
from watcher.metrics import MetricsTail

OnFailure = Callable[[Detection, "Runner"], None]
KILL_ON = {"nan", "oom"}  # stop wasting compute on these


class Runner:
    def __init__(self, cmd: list[str], run_dir: str | Path, on_failure: OnFailure,
                 detectors: Optional[Detectors] = None, log_tail_lines: int = 200,
                 poll_s: float = 0.5, cwd: str | Path | None = None, echo: bool = False):
        self.cmd = cmd
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = self.run_dir.name
        self.on_failure = on_failure
        self.det = detectors or Detectors()
        self.log_tail: deque[str] = deque(maxlen=log_tail_lines)
        self.poll_s = poll_s
        self.cwd = cwd
        self.echo = echo  # mirror training output to our stdout (live demo view)
        self.proc: Optional[subprocess.Popen] = None
        self.detections: list[Detection] = []
        self._lock = threading.Lock()

    @property
    def metrics_path(self) -> Path:
        return self.run_dir / "metrics.jsonl"

    def _handle(self, d: Optional[Detection]) -> None:
        if d is None:
            return
        with self._lock:
            self.detections.append(d)
        if d.type in KILL_ON and self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.on_failure(d, self)

    def _read_output(self) -> None:
        assert self.proc and self.proc.stdout
        with (self.run_dir / "train.log").open("a") as log_file:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self.log_tail.append(line)
                log_file.write(line + "\n")
                log_file.flush()
                if self.echo:
                    print(line, flush=True)
                self._handle(self.det.feed_line(line))

    def run(self) -> int:
        popen_kwargs: dict = dict(
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        if sys.platform == "win32":
            # Avoid flashing a console window when launched from a GUI/agent host.
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self.cmd, **popen_kwargs)
        reader = threading.Thread(target=self._read_output, daemon=True)
        reader.start()
        tail = MetricsTail(self.metrics_path)
        while self.proc.poll() is None:
            for row in tail.new_rows():
                self._handle(self.det.feed_metric(row))
            time.sleep(self.poll_s)
        reader.join(timeout=5)
        for row in tail.new_rows():
            self._handle(self.det.feed_metric(row))
        code = self.proc.returncode
        if not any(d.type in KILL_ON for d in self.detections):
            self._handle(self.det.feed_exit(code))
        return code
