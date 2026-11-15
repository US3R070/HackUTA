# Proactive Training Agent — judge demo

## What these files do

`train.py` supplies the real training process for section 9: numeric loss,
stdout/stderr, JSONL metrics, checkpoints, deliberate instability, and recovery.
It uses NumPy float32 and manual stochastic gradient descent (SGD) to train a
small one-layer regression network on synthetic data. No dataset download,
GPU, or PyTorch installation is needed. Both supplied YAML files work unchanged.

The watcher, Gemini diagnosis, Chert iMessage/FaceTime, ElevenLabs voice, live
screen publishing, approval handling, Git changes, and final report belong to
the other modules in the spec. This script does not implement or simulate them.

## Setup and rehearsal

Run these commands from the repository root (the folder containing `demo/`).
On Windows, use Python 3.12 for this demo. The project also uses LiveKit's
Google plugin, which requires NumPy below 2; Python 3.12 has compatible NumPy
1.x wheels. Use the same virtual-environment interpreter for the watcher and
its training subprocess.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install "numpy<2" PyYAML
.\.venv\Scripts\python.exe -c "import numpy, yaml; print('NumPy', numpy.__version__, '| PyYAML', yaml.__file__)"
```

Run training by itself:

```powershell
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/judge-demo-01
```

For the local integrated watcher demo, let the watcher launch training; do not
start a second training process. Use a fresh run directory for each rehearsal:

```powershell
.\.venv\Scripts\python.exe -m watcher.runner --run-dir runs/judge-demo-01 --config demo/configs/bad_lr.yaml --cwd . -- .\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/judge-demo-01
```

To rehearse the failure quickly, add `--step-delay 0 --warmup-steps 0` to the
training command (after `train.py` and its required arguments). For example:

```powershell
.\.venv\Scripts\python.exe -m watcher.runner --run-dir runs/local-fast --config demo/configs/bad_lr.yaml --cwd . -- .\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/local-fast --step-delay 0 --warmup-steps 0
```

The watcher returns the training process's exit code. Exit code 2 is the
expected numerical failure; inspect `runs/<run-id>/incidents/` and
`runs/<run-id>/watcher-*.log` for the incident and watcher output.

### Why failure happens near two minutes

The first 1,000 steps use `min(configured_lr, 0.001)`. Then the configured LR
applies. At LR 5.0, SGD overshoots repeatedly and float32 arithmetic overflows.
Each step sleeps 0.117 seconds, giving approximately two minutes before failure
on a typical laptop. File I/O and machine speed add overhead: rehearse locally.
These are explicit presentation controls; no fabricated loss or injected NaN
is used. The first non-finite result can be **Inf rather than NaN**. Both are
training failures and should trigger the spec's `nan` detector with HIGH severity.
Stopping at the first non-finite value protects the last checkpoint.

To shorten rehearsal, add `--step-delay 0`. To disable the initial safe phase,
add `--warmup-steps 0`; failure will occur much earlier. With `good.yaml`, LR
stays 0.001 throughout. Total `steps` means the final global step, not the number
of extra steps to run after resuming.

### Before the judges arrive

1. Verify iMessage and FaceTime delivery using a consenting test recipient.
2. Verify the FaceTime participant can see the laptop screen and hear the agent.
3. Show a terminal/log view and a loss plot. Use a log-scale loss axis so the
   stable region remains visible next to the divergence spike.
4. Ensure the watcher understands the strict JSON failure event described below.
5. Ensure the fixer requires the judge's explicit approval before editing/resuming.
6. Start training around two minutes before the live incident segment. The
   section's “60s” refers to the incident story, not the entire training wait.
7. Use a fresh run directory each rehearsal. Keep just one writer per directory.

## Live script — approximately 60 seconds after the alert

| Time | Presenter / judge | System behavior to demonstrate |
| --- | --- | --- |
| 0–10 s | Presenter: “I started a training run and stepped away. If it fails, our agent reaches me wherever I am.” | The watcher detects non-finite loss; the phone receives the incident iMessage. |
| 10–20 s | Presenter: “This is a high-severity failure, so it calls.” Judge answers. | Chert bridges FaceTime into LiveKit. Agent voice and live laptop screen appear. |
| 20–35 s | Suggested agent explanation: “The training loss became non-finite after the learning rate rose to 5.0. The updates became unstable. I can reduce it to 0.001 and restart from the last checkpoint. May I do that?” | Show logs, then the loss curve rising sharply. Ground the spoken diagnosis in the actual incident; do not invent a step number. |
| 35–45 s | Judge: “Yes, do it.” | Fixer creates `agent/fix-<incident_id>`, changes LR, and runs a short isolated validation. |
| 45–60 s | Presenter: “You can watch the change and recovery live.” | Fixer resumes, verifies finite improving loss over a window, shows the recovering curve, and sends the before/after report. |

Service latency may extend this sequence. Say “training has resumed and is
stable so far” after a short verification window; do not claim all 5,000 steps
have completed. If a provider fails, label any terminal-only demonstration as a
fallback rather than implying a call or text was delivered.

Suggested closing line: “Our agent watches your runs, calls when they break,
shows you the screen live, and fixes the problem with your OK.”

## Fixer integration and manual recovery

The commands below are the concrete subprocess interfaces the fixer can use.
They are also useful for testing without the phone stack.

First, after explicit approval, create the fix branch and change only `lr` in
`demo/configs/bad_lr.yaml` from `5.0` to `0.001`. Keep the other values unchanged.
`good.yaml` provides an already-correct configuration for standalone tests.

### 1. Dry-run the patched configuration in a NEW directory

```powershell
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/judge-dry-run-01 --resume runs/judge-demo-01/checkpoints/latest.npz --step-delay 0 --max-steps 100
```

Before this command, patch the config as above. Exit code 0 means the requested
100 additional steps finished without a detected numerical failure. Inspect
loss/validation loss as well; exit code alone does not prove improvement.
The dry run writes its own metrics and checkpoints, leaving the original
incident artifacts intact. Rehearsals need a fresh dry-run directory too.

### 2. Resume the original run with the approved configuration

```powershell
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/bad_lr.yaml --run-dir runs/judge-demo-01 --resume runs/judge-demo-01/checkpoints/latest.npz
```

Or, for standalone recovery using the supplied good file:

```powershell
.\.venv\Scripts\python.exe -u demo/train.py --config demo/configs/good.yaml --run-dir runs/judge-demo-01 --resume runs/judge-demo-01/checkpoints/latest.npz
```

The checkpoint restores weights, global step, and dataset seed. This optimizer
is plain SGD, so there is no momentum or adaptive state to restore. **The LR
comes from the new YAML**, and resume skips warm-up. Resume the last safe
checkpoint, not the overflowing parameters. For the default seed/configuration,
the periodic checkpoint before divergence is expected at step 1,000; read the
checkpoint/status instead of hardcoding that assumption in the fixer.

## Watcher, plotting, and report contract

Each run directory contains:

| File | Purpose |
| --- | --- |
| `metrics.jsonl` | Flushed records containing `step`, `loss`, `val_loss`, `lr`, `time`, `attempt`, and `event`. |
| `train.log` | Flushed human-readable training output, also emitted to stdout. Capture stderr separately for unexpected exceptions. |
| `status.json` | Atomically updated process status and checkpoint path. |
| `config-<attempt>.json` | Configuration and presentation settings for each launch/resume attempt. |
| `checkpoints/latest.npz` | Last saved finite state; atomically replaced. |
| `checkpoints/step_<step>_<attempt>.npz` | Versioned periodic and final checkpoints. |

Failure emits a metric immediately, even between scheduled logging steps:

```json
{"step": 1022, "loss": null, "val_loss": null, "lr": 5.0, "time": 120.0, "attempt": "example", "event": "non_finite", "non_finite": true, "loss_text": "inf", "val_loss_text": "inf"}
```

This record is illustrative; actual step/time/value details come from the run.
The watcher must detect `event == "non_finite"` or `non_finite == true`;
`math.isnan(row["loss"])` alone is insufficient because JSON encodes missing
non-finite numbers as `null`. Also detect the log text `NaN/Inf detected` and
process exit code 2. Group those signals into one incident, not three calls.

Exit codes: 0 = completed or intentionally paused after `--max-steps`;
2 = numerical failure; 130 = keyboard interruption; unexpected errors are
nonzero with traceback on stderr. `status.json` is supplementary: startup errors
may occur before it is updated, so the watcher must monitor process exits too.

Resume appends records with a **new attempt ID**. Global steps rewind to the
checkpoint, and `time` restarts at zero for each attempt. Plot separate series
per attempt (preserve file order); do not assume global step or time is strictly
increasing across resumes. Skip null values and annotate the failure event.
Compare early and late validation-loss windows for recovery, rather than
requiring every random minibatch loss to decrease.

The agent should log approval, patch, dry-run result, restart, and verification
in `agent_log.jsonl` as required by the spec; that file belongs to the agent,
not the training process. Build the before/after report from the original
failed attempt and the new recovery attempt, with their actual values.
