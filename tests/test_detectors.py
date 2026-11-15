"""Detector tests."""
import math

from watcher.detectors import Detectors


def test_nan_metric():
    d = Detectors()
    assert d.feed_metric({"step": 10, "loss": 1.0}) is None
    det = d.feed_metric({"step": 20, "loss": math.nan})
    assert det and det.type == "nan" and det.step == 20


def test_nan_fires_once():
    d = Detectors()
    assert d.feed_metric({"step": 1, "loss": math.nan})
    assert d.feed_metric({"step": 2, "loss": math.nan}) is None


def test_oom_line():
    det = Detectors().feed_line("RuntimeError: CUDA out of memory. Tried to allocate 2 GiB")
    assert det and det.type == "oom"


def test_crash_with_traceback():
    d = Detectors()
    for line in ["Traceback (most recent call last):", '  File "x.py", line 3', "RuntimeError: boom"]:
        d.feed_line(line)
    det = d.feed_exit(1)
    assert det and det.type == "crash" and "RuntimeError: boom" in det.traceback


def test_clean_exit_no_crash():
    assert Detectors().feed_exit(0) is None


def test_plateau():
    d = Detectors(plateau_window=100, plateau_min_improvement=0.01)
    out = None
    for s in range(0, 300, 10):
        out = out or d.feed_metric({"step": s, "loss": 3.0 - s * 0.01 if s < 100 else 2.0})
    assert out and out.type == "plateau"


def test_overfit():
    d = Detectors(overfit_window=3)
    out = None
    for i, (tr, va) in enumerate([(2.0, 1.0), (1.8, 1.1), (1.6, 1.2), (1.4, 1.3)]):
        out = out or d.feed_metric({"step": i, "loss": tr, "val_loss": va})
    assert out and out.type == "overfit"