"""Fake training for building/testing the watcher before demo/train.py exists.

  python3 demo/fake_train.py --run-dir runs/test --mode nan --after 3
modes: healthy | nan | crash | oom | plateau
"""
import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
p.add_argument("--mode", default="nan", choices=["healthy", "nan", "crash", "oom", "plateau"])
p.add_argument("--after", type=float, default=5.0, help="seconds before failure")
p.add_argument("--steps", type=int, default=100000)
p.add_argument("--dt", type=float, default=0.05)
a = p.parse_args()

run = Path(a.run_dir)
run.mkdir(parents=True, exist_ok=True)
mf = (run / "metrics.jsonl").open("a")
t0 = time.time()
loss = 3.0
for step in range(0, a.steps, 10):
    el = time.time() - t0
    failing = el >= a.after
    if a.mode == "plateau":
        loss = 1.0 + random.uniform(-0.002, 0.002) if failing else loss * 0.97
    else:
        loss = max(0.05, loss * 0.98 + random.uniform(-0.01, 0.01))
    if failing and a.mode == "nan":
        loss = math.nan
    if failing and a.mode == "crash":
        raise RuntimeError("shape mismatch: expected [64, 128], got [64, 256]")
    if failing and a.mode == "oom":
        print("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB", flush=True)
        sys.exit(1)
    print(f"step {step} loss {loss:.4f}", flush=True)
    mf.write(json.dumps({"step": step, "loss": loss, "val_loss": None, "lr": 0.01, "time": el}) + "\n")
    mf.flush()
    if a.mode == "healthy" and el > a.after:
        break
    time.sleep(a.dt)