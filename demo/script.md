# Local training watcher demo

This guide describes the current `watcher.Detectors`, `watcher.MetricsTail`,
and `watcher.Runner` implementation and the synthetic trainer in `train.py`.
The trainer uses NumPy float32 and manual SGD on fixed synthetic data. It needs
NumPy and PyYAML, but no dataset download, GPU, or PyTorch.

The current `Runner` is a Python API, not a command-line module. Use
`main.py` to launch it with incident building, Grok triage, and the registered
notification/escalation handlers. This launch flow does not automatically
apply a code fix; the agent must get the user's approval before any fix is
applied.

## Environment setup (Windows PowerShell)

Run from the repository root. Use the project's `.venv` interpreter for both
the watcher process and its training subprocess. Python 3.12 is recommended on
Windows for the NumPy 1.x wheel used by the project.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install "numpy<2" PyYAML
.\.venv\Scripts\python.exe -c "import numpy, yaml; print('NumPy', numpy.__version__, '| PyYAML', yaml.__file__)"
```

If `.venv` already exists, skip the `venv` creation command. For the full
application environment, install `requirements.txt` into the same `.venv` and
keep NumPy below version 2 to satisfy `livekit-plugins-google`.

## Run training without the watcher

Use a new run directory each time unless resuming from a checkpoint:

```powershell
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/train-only --step-delay 0 --warmup-steps 0
```

`bad_lr.yaml` sets LR to 5.0. The warm-up normally caps LR at 0.001 for the
first `--warmup-steps` steps; its default is now zero. With the deliberately
unstable model code and LR 5.0, the trainer is intended to fail within a
minute. It stops at the first non-finite update, writes a failure metric,
reports the failure, and returns exit code 2.
`good.yaml` uses LR 0.001 and should complete without a numerical failure.

## Run through the application launcher

This starts the watcher and launches the model in `train.py`. `main.py` creates
a timestamped run directory and substitutes it for `"{run_dir}"`. It also
builds and saves the incident, requests Grok triage (or uses fallback triage),
and invokes the registered notification and escalation handlers. Install the
full `requirements.txt` dependencies and configure the required service values
in the repository `.env` before using external notifications or calls.

```powershell
.\.venv\Scripts\python.exe main.py --config demo/configs/bad_lr.yaml -- .\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir "{run_dir}"
```

This is the interactive application path. Use the direct command above when
you only want to run the training process without triage or notification.

## Run the current watcher API

`Runner` takes the child command as a list and requires an `on_failure`
callback. This PowerShell command runs the bad-LR demo quickly and prints each
detection. Pick a fresh run directory for each run:

```powershell
.\.venv\Scripts\python.exe -c "import sys; from watcher.runner import Runner; r=Runner([sys.executable, '-u', 'demo/train.py', '--config', 'demo/configs/bad_lr.yaml', '--run-dir', 'runs/watcher-fast', '--step-delay', '0', '--warmup-steps', '0'], 'runs/watcher-fast', lambda d, r: print(f'DETECTED {d.type} step={d.step}: {d.detail}')); print('child exit code:', r.run())"
```

The `Runner` merges child stdout and stderr, appends the output to
`runs/watcher-fast/train.log`, tails `metrics.jsonl`, and invokes the callback
for each detection type at most once. It terminates the child on `nan` and
`oom`; therefore the final child exit code may reflect termination rather than
the trainer's own exit code 2. The callback receives a `Detection` and the
`Runner`. The caller must build/persist an incident or connect notification
services if desired; `Runner` itself only keeps detections in memory.
The trainer also appends to `train.log`, so lines captured by `Runner` may be
duplicated because both processes write to that file.

To use the normal two-minute presentation timing, remove `--step-delay 0` and
`--warmup-steps 0` from the child command. Do not start a second trainer for
the same run directory.

## What the watcher detects

`Detectors` reports these types:

| Type | Detection rule |
| --- | --- |
| `nan` | A parsed metric has numeric NaN/Inf, or a log line contains `loss` followed by `nan` or `inf`. `train.py` emits null loss values plus a `non_finite` event, so its `ERROR NaN/Inf detected...` log line is what the current detector recognizes. |
| `oom` | A log line contains CUDA/MPS out-of-memory wording, `OutOfMemoryError`, or `out of memory`. |
| `crash` | The process exits nonzero and no `nan`/`oom` kill detection suppressed exit classification. |
| `plateau` | Loss improvement over the configured step window is below the relative threshold. Defaults: 500 steps and 1% minimum relative improvement. |
| `overfit` | Across six validation records by default, validation loss rises at every record while training loss falls from first to last. |

Each type fires at most once per `Detectors` instance. `Runner` terminates the
child for `nan` and `oom`; it does not stop the child for plateau or overfit.
The demo's immediate non-finite metric stores `null` for invalid loss values.
The current detector does not inspect the event/flag fields, so detection of
this demo failure relies on its error log line.

`MetricsTail.new_rows()` reads appended newline-delimited JSON incrementally.
It buffers an incomplete final line until a newline arrives and skips blank or
malformed lines. It does not currently reset its read position if the metrics
file is truncated or replaced.

## Files written by `train.py`

| File | Contents |
| --- | --- |
| `metrics.jsonl` | Metric rows with `step`, `loss`, `val_loss`, `lr`, `time`, `attempt`, and `event`; the non-finite failure row also has `non_finite: true` and text forms of the invalid values. |
| `train.log` | Training messages, also printed to stdout. Under `Runner`, stdout and stderr are merged and appended to this file. |
| `status.json` | Atomically replaced process status, current step, attempt, and checkpoint path. |
| `config-<attempt>.json` | Effective config and presentation settings for each launch/resume attempt. |
| `checkpoints/latest.npz` | Last finite checkpoint, atomically replaced. |
| `checkpoints/step_<step>_<attempt>.npz` | Periodic and final finite checkpoints. |

The failure metric is emitted immediately, even between normal log intervals.
Its exact step, elapsed time, and invalid value depend on the run. The checkpoint
contains only finite weights. The optimizer is plain SGD, so there is no
momentum/adaptive optimizer state to restore.

## Recovery

After explicit approval to change the config, set `lr` from `5.0` to `0.001`
in `demo/configs/bad_lr.yaml`, or use the supplied `good.yaml`. Resume the
original run from its latest safe checkpoint. Use the same run directory so
the new attempt appends metrics, and choose a fresh directory for a dry run:

```powershell
# Dry-run 100 additional steps in a new directory:
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/good.yaml --run-dir runs/recovery-dry-run --resume runs/watcher-fast/checkpoints/latest.npz --step-delay 0 --max-steps 100

# Resume the original run:
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/good.yaml --run-dir runs/watcher-fast --resume runs/watcher-fast/checkpoints/latest.npz
```

`steps` in the YAML is the final global step. `--max-steps` limits additional
steps for a dry run. Resume skips warm-up and uses the LR from the new config.
The checkpoint restores the weights, global step, and dataset seed. Inspect
the actual checkpoint/status to find the last safe step; do not assume a fixed
step number.

Do not start a fresh `Runner` on a resumed run directory with the current
implementation: its metrics tail begins at byte zero, so it can replay old
failure rows and re-detect the prior incident. Run a resumed training process
directly as shown above, or use a separate fresh run directory when exercising
the watcher.

## Reading recovery metrics

Every resume gets a new `attempt` ID, steps continue from the saved checkpoint,
and elapsed `time` starts over. Group plots by `attempt` and preserve file
order. Skip null loss values and mark the non-finite event. Compare early and
late validation-loss windows to judge recovery instead of requiring every
random minibatch loss to decrease.
