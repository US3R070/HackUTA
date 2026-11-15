#!/usr/bin/env bash
# Hands-off FaceTime escalate: no TUI approve prompts (you're on the phone).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="${HOME}/.grok/bin:${PATH}"
# Interactive TUI (don't use -p); always-approve so you're not stuck at the keyboard.
exec grok --always-approve --fullscreen "$(cat demo/hands_off_escalate_prompt.txt)"
