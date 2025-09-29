#!/usr/bin/env bash
set -Eeuo pipefail
uv run shell_emulator.py --vfs "./vfs/three_levels.xml" --script "./scripts/demo_stage4.emu"
