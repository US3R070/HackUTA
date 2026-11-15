
"""Detectors: crash / nan / oom / plateau / overfit. Owner: Binoy."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

OOM_RE = re.compile(r"(CUDA out of memory|MPS backend out of memory|OutOfMemoryError|out of memory)", re.I)
NAN_LOG_RE = re.compile(r"loss[^\n]*?\b(nan|inf)\b", re.I)
TRACEBACK_START = "Traceback (most recent call last):"


@dataclass
class Detection:
    type: str
    step: Optional[int] = None
    traceback: Optional[str] = None
    detail: str = ""


def _bad(x: Any) -> bool:
    return isinstance(x, (int, float)) and (math.isnan(x) or math.isinf(x))


@dataclass
class Detectors:
    plateau_window: int = 500
    plateau_min_improvement: float = 0.01
    overfit_window: int = 5
    _last_step: Optional[int] = None
    _tb_lines: list[str] = field(default_factory=list)
    _in_tb: bool = False
    _rows: list[dict[str, Any]] = field(default_factory=list)
    _fired: set[str] = field(default_factory=set)

    # ---- log lines ----
    def feed_line(self, line: str) -> Optional[Detection]:
        if line.startswith(TRACEBACK_START):
            self._in_tb, self._tb_lines = True, [line]
        elif self._in_tb:
            self._tb_lines.append(line)
        if OOM_RE.search(line):
            return self._fire(Detection("oom", self._last_step, self.traceback, line.strip()))
        if NAN_LOG_RE.search(line):
            return self._fire(Detection("nan", self._last_step, None, line.strip()))
        return None

    @property
    def traceback(self) -> Optional[str]:
        return "\n".join(self._tb_lines) if self._tb_lines else None

    # ---- metrics ----
    def feed_metric(self, row: dict[str, Any]) -> Optional[Detection]:
        self._last_step = row.get("step", self._last_step)
        self._rows.append(row)
        if _bad(row.get("loss")) or _bad(row.get("val_loss")):
            return self._fire(Detection("nan", self._last_step, None, f"loss={row.get('loss')}"))
        return self._check_plateau() or self._check_overfit()

    def _check_plateau(self) -> Optional[Detection]:
        rows = [r for r in self._rows if isinstance(r.get("loss"), (int, float)) and "step" in r]
        if len(rows) < 2 or rows[-1]["step"] - rows[0]["step"] < 2 * self.plateau_window:
            return None
        cutoff = rows[-1]["step"] - self.plateau_window
        before = [r["loss"] for r in rows if r["step"] <= cutoff]
        recent = [r["loss"] for r in rows if r["step"] > cutoff]
        if not before or not recent:
            return None
        best_before, best_recent = min(before), min(recent)
        if best_before <= 0:
            return None
        improvement = (best_before - best_recent) / abs(best_before)
        if improvement < self.plateau_min_improvement:
            return self._fire(Detection("plateau", self._last_step, None,
                                        f"improvement {improvement:.4f} over last {self.plateau_window} steps"))
        return None

    def _check_overfit(self) -> Optional[Detection]:
        vals = [r for r in self._rows if isinstance(r.get("val_loss"), (int, float))]
        n = self.overfit_window
        if len(vals) < n + 1:
            return None
        recent = vals[-(n + 1):]
        val_up = all(b["val_loss"] > a["val_loss"] for a, b in zip(recent, recent[1:]))
        train_down = recent[-1].get("loss", math.inf) < recent[0].get("loss", -math.inf)
        if val_up and train_down:
            return self._fire(Detection("overfit", self._last_step, None, "val_loss rising while loss falls"))
        return None

    # ---- process exit ----
    def feed_exit(self, code: int) -> Optional[Detection]:
        if code != 0:
            return self._fire(Detection("crash", self._last_step, self.traceback, f"exit code {code}"))
        return None

    def _fire(self, d: Detection) -> Optional[Detection]:
        if d.type in self._fired:
            return None
        self._fired.add(d.type)
        return d