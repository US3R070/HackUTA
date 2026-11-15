"""Assemble Incident: traceback, log tail, metrics, config, git diff, source. Owner: Binoy."""
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from incident.models import Incident
from watcher.detectors import Detection
from watcher.metrics import read_metrics

TB_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')


def git_diff(repo_dir: str | Path = ".", max_chars: int = 8000) -> str:
    try:
        out = subprocess.run(["git", "diff", "HEAD"], cwd=repo_dir, capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return ""
    return out[:max_chars]


def load_config(path: Optional[str | Path]) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text()) or {}


def source_snippets(traceback: Optional[str], context: int = 15, max_files: int = 3) -> dict[str, str]:
    """Code around each traceback frame in the user's repo (skips site-packages)."""
    if not traceback:
        return {}
    out: dict[str, str] = {}
    for path, line in reversed(TB_FILE_RE.findall(traceback)):
        p = Path(path)
        if "site-packages" in path or not p.exists() or path in out:
            continue
        lines = p.read_text().splitlines()
        n = int(line)
        lo, hi = max(0, n - context - 1), min(len(lines), n + context)
        out[path] = "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))
        if len(out) >= max_files:
            break
    return out


def build_incident(det: Detection, run_id: str, log_tail: Iterable[str], metrics_path: str | Path,
                   config_path: Optional[str | Path] = None, repo_dir: str | Path = ".",
                   metrics_n: int = 50) -> Incident:
    return Incident(
        run_id=run_id,
        type=det.type,
        step=det.step,
        traceback=det.traceback,
        log_tail=list(log_tail),
        metrics_tail=read_metrics(metrics_path, last_n=metrics_n),
        config=load_config(config_path),
        git_diff=git_diff(repo_dir),
        source=source_snippets(det.traceback),
    )
