"""Pure detectors: no network calls or file mutation."""
from collections import deque
import math
import re



def non_finite(row):
    if row.get('event') == 'non_finite' or row.get('non_finite') is True:
        return True
    
    for key in ('loss', 'val_loss'):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            return True
        if isinstance(value, str) and value.lower() in {'nan', 'inf', '-inf', 'infinity', '-infinity'}:
            return True
    
    return False  # A null val_loss alone means validation was not measured.



def log_failure(line):
    if re.search(r'out of memory|OutOfMemoryError|MemoryError|CUDA_ERROR_OUT_OF_MEMORY', line, re.I):
        return 'oom'
    
    if re.search(r'NaN/Inf detected|(?:loss|val_loss)\s*[:=]\s*[+-]?(?:nan|inf(?:inity)?)\b', line, re.I):
        return 'nan'
  
    return None  # Tracebacks are captured; a nonzero process exit confirms crash.



class Plateau:
    """No new best loss by min_delta for patience GLOBAL STEPS, not records.

    Disabled with patience=0. Resets on new attempts and step rewinds.
    Prefers validation loss; changing metric also resets its baseline.
    """
    def __init__(self, patience=0, min_delta=0.001):
        self.patience = patience
        self.min_delta = min_delta
        self.identity = None
        self.last_step = -1
        self.best = math.inf
        self.best_step = None


    def update(self, row):
        if not self.patience or non_finite(row):
            return False

        key = 'val_loss' if row.get('val_loss') is not None else 'loss'
        value, step = row.get(key), row.get('step')
        
        if type(step) is not int or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return False
        
        identity = (row.get('attempt'), key)
        if identity != self.identity or step < self.last_step:
            self.best, self.best_step = math.inf, None

        self.identity, self.last_step = identity, step
        if self.best_step is None or value < self.best - self.min_delta:
            self.best, self.best_step = value, step
        
        return step - self.best_step >= self.patience
