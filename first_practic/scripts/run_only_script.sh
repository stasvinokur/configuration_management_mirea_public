#!/usr/bin/env bash
set -Eeuo pipefail
uv run shell_emulator.py --script "./scripts/demo_fail.emu"
