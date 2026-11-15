#!/usr/bin/env python3
"""Small, real float32 regression demo; no downloads, GPU, or API keys needed.

Run from the repository root. See script.md for watcher/fixer integration.
The warm-up and sleep are deliberate presentation controls, not fake losses.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import yaml


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--resume', type=Path, help='Trusted local checkpoint .npz')
    parser.add_argument('--warmup-steps', type=int, default=0)
    parser.add_argument('--step-delay', type=float, default=0.117,
                        help='Seconds to sleep each step; use 0 for fast tests')
    parser.add_argument('--max-steps', type=int,
                        help='Stop after this many additional steps (dry run)')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.warmup_steps < 0 or not math.isfinite(args.step_delay) or args.step_delay < 0:
        parser.error('warmup-steps and step-delay must be nonnegative')
    if args.max_steps is not None and args.max_steps <= 0:
        parser.error('max-steps must be positive')
    return args


def read_config(path):
    with path.open(encoding='utf-8') as handle:
        config = yaml.safe_load(handle)
    required = {'lr', 'batch_size', 'steps', 'log_every', 'checkpoint_every'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError(f'Config must contain exactly: {sorted(required)}')
    for name in required - {'lr'}:
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError(f'{name} must be a positive integer')
    if isinstance(config['lr'], bool) or not isinstance(config['lr'], (int, float)):
        raise ValueError('lr must be a number')
    if not math.isfinite(config['lr']) or config['lr'] <= 0:
        raise ValueError('lr must be finite and positive')
    return config


def write_json(path, value):
    """Atomic replacement lets another process safely read status.json."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def save_checkpoint(path, weights, step, seed):
    """Only called for validated finite state. SGD has no momentum state here."""
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as handle:
        np.savez(handle, weights=weights, step=np.int64(step), seed=np.int64(seed))
    os.replace(temporary, path)


def main():
    args = arguments()
    config = read_config(args.config)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.run_dir / 'metrics.jsonl'
    if metrics_path.exists() and not args.resume:
        raise ValueError('Run already exists: choose a fresh --run-dir or use --resume')

    # Appending a constant feature is equivalent to a trainable bias.
    # The softplus output is a poor fit for regression targets that can be
    # negative, and the update below intentionally uses the ascent direction.
    # This creates a reproducible code-level failure for the watcher demo.
    seed = args.seed
    weights = np.zeros((9, 1), dtype=np.float32)
    start_step = 0
    if args.resume:
        # No pickle loading: the checkpoint contains numeric arrays only.
        with np.load(args.resume, allow_pickle=False) as saved:
            weights = saved['weights'].copy()
            start_step = int(saved['step'])
            seed = int(saved['seed'])
        if weights.shape != (9, 1) or weights.dtype != np.float32:
            raise ValueError('Checkpoint does not match this model')
        if not np.isfinite(weights).all() or start_step < 0:
            raise ValueError('Checkpoint is not a valid finite training state')
    if start_step >= config['steps']:
        raise ValueError('Checkpoint has already reached configured total steps')

    # Fixed synthetic train/validation data. The model learns a real mapping.
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(2048, 8)).astype(np.float32)
    x = np.concatenate([x, np.ones((2048, 1), dtype=np.float32)], axis=1)
    target_weights = np.linspace(-1, 1, 9, dtype=np.float32).reshape(9, 1)
    y = x @ target_weights + rng.normal(0, 0.05, (2048, 1)).astype(np.float32)
    train_x, val_x = x[:1792], x[1792:]
    train_y, val_y = y[:1792], y[1792:]

    checkpoint_dir = args.run_dir / 'checkpoints'
    checkpoint_dir.mkdir(exist_ok=True)
    latest = checkpoint_dir / 'latest.npz'
    # Step 0 provides a recovery point even with warm-up disabled.
    save_checkpoint(latest, weights, start_step, seed)
    attempt = uuid.uuid4().hex[:12]
    metadata = dict(attempt=attempt, config=config, config_path=str(args.config.resolve()),
                    resumed_from=str(args.resume) if args.resume else None,
                    warmup_steps=args.warmup_steps, step_delay=args.step_delay,
                    seed=seed, started_at=datetime.now(timezone.utc).isoformat())
    write_json(args.run_dir / f'config-{attempt}.json', metadata)
    started = time.monotonic()
    stop_step = min(config['steps'], start_step + args.max_steps) if args.max_steps else config['steps']

    with (args.run_dir / 'train.log').open('a', encoding='utf-8', buffering=1) as log, \
            metrics_path.open('a', encoding='utf-8', buffering=1) as metrics:
        def say(message):
            print(message, flush=True)
            log.write(message + '\n')

        def status(state, step, **extra):
            write_json(args.run_dir / 'status.json', dict(
                status=state, step=step, attempt=attempt,
                checkpoint=str(latest.resolve()), **extra))

        def record(step, loss, val_loss, lr, event='metric', **extra):
            # JSON disallows NaN/Infinity. Null + explicit metadata preserves
            # strict JSON compatibility for browsers, Gemini, and the watcher.
            row = dict(step=step, loss=loss if math.isfinite(loss) else None,
                       val_loss=val_loss if math.isfinite(val_loss) else None,
                       lr=lr, time=round(time.monotonic() - started, 3),
                       attempt=attempt, event=event, **extra)
            metrics.write(json.dumps(row, allow_nan=False) + '\n')

        say(f'START attempt={attempt} step={start_step} configured_lr={config["lr"]}')
        say(f'DEMO warmup={args.warmup_steps} steps; delay={args.step_delay}s/step')
        status('running', start_step)
        try:
            for step in range(start_step + 1, stop_step + 1):
                time.sleep(args.step_delay)
                # Optional warm-up is available for longer rehearsals. By
                # default, use the configured learning rate immediately so
                # this intentionally unstable model fails within a minute.
                # On resume, use the NEW config immediately, without warm-up.
                lr = min(config['lr'], 0.001) if not args.resume and step <= args.warmup_steps else config['lr']
                # Step-derived batches make replay independent of process RNG state.
                batch_rng = np.random.default_rng(seed + step)
                indices = batch_rng.integers(0, len(train_x), size=config['batch_size'])
                bx, by = train_x[indices], train_y[indices]
                with np.errstate(over='ignore', invalid='ignore'):
                    logits = bx @ weights
                    prediction = np.logaddexp(np.float32(0.0), logits)
                    output_gradient = 1.0 / (1.0 + np.exp(-logits))
                    error = prediction - by
                    gradient = (np.float32(2.0 / len(bx)) * bx.T) @ (error * output_gradient)
                    # Deliberately bad optimizer direction: this adds the
                    # loss gradient, so high loss grows until float32 fails.
                    candidate = weights + np.float32(lr) * gradient
                    train_prediction = np.logaddexp(np.float32(0.0), bx @ candidate)
                    val_prediction = np.logaddexp(np.float32(0.0), val_x @ candidate)
                    loss = float(np.mean((train_prediction - by) ** 2))
                    val_loss = float(np.mean((val_prediction - val_y) ** 2))
                if not (math.isfinite(loss) and math.isfinite(val_loss) and np.isfinite(candidate).all()):
                    record(step, loss, val_loss, lr, 'non_finite',
                           non_finite=True, loss_text=str(loss), val_loss_text=str(val_loss))
                    say(f'ERROR NaN/Inf detected step={step} loss={loss} val_loss={val_loss} lr={lr}')
                    say(f'RECOVER from {latest}; do not resume the diverged weights')
                    status('failed', step, reason='non_finite', exit_code=2)
                    return 2
                weights = candidate
                if step % config['log_every'] == 0 or step == start_step + 1 or step == stop_step:
                    record(step, loss, val_loss, lr)
                    say(f'step={step} loss={loss:.6g} val_loss={val_loss:.6g} lr={lr:g}')
                if step % config['checkpoint_every'] == 0 or step == stop_step:
                    # Checkpoints have already passed both train and val checks.
                    save_checkpoint(latest, weights, step, seed)
                    save_checkpoint(checkpoint_dir / f'step_{step:06d}_{attempt}.npz', weights, step, seed)
                status('running', step)
        except KeyboardInterrupt:
            say('INTERRUPTED: last periodic checkpoint remains available')
            status('interrupted', step)
            return 130
        state = 'completed' if stop_step == config['steps'] else 'paused'
        status(state, stop_step, exit_code=0)
        say(f'{state.upper()} step={stop_step}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # stderr + nonzero exit lets the watcher classify unexpected crashes.
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise
