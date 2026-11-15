"""Incremental JSONL reading, including lines split across file writes."""
import json
from pathlib import Path



class MetricsTail:
    def __init__(self, path, start_at_end=False):
        self.path = Path(path)
        self.offset = self.path.stat().st_size if start_at_end and self.path.exists() else 0
        self.pending = b''
        self.errors = []


    def read(self):
        self.errors = []
        try:
            with self.path.open('rb') as stream:
                if self.path.stat().st_size < self.offset:
                    self.offset = 0
                    self.pending = b''
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            return []
        
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()  # Hold incomplete last line until next poll.
        rows = []

        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('Metric must be a JSON object')
                rows.append(row)
            except (ValueError, UnicodeDecodeError) as exc:
                self.errors.append(str(exc))
        return rows
