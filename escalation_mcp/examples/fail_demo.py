"""Standalone demo that fails after ~20s so a Grok bot can escalate it.

Does not depend on demo/train.py.
"""

from __future__ import annotations

import math
import sys
import time


def main() -> int:
    print("fail_demo: starting fake training loop", flush=True)
    lr = 5.0
    loss = 2.5
    for step in range(1, 50):
        time.sleep(0.4)
        # Blow up around step ~40 (~16s)
        if step >= 35:
            loss = float("nan") if step % 2 == 0 else float("inf")
        else:
            loss = max(0.1, loss * 0.97 + (0.01 * math.sin(step)))
        print(f"step={step} loss={loss} lr={lr}", flush=True)
        if not math.isfinite(loss):
            print(
                f"ERROR: NaN/Inf loss at step {step}. Learning rate {lr} looks too high.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
