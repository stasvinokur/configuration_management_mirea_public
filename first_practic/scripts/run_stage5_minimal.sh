#!/usr/bin/env bash
set -Eeuo pipefail
uv run shell_emulator.py --vfs "./vfs/minimal.xml" --script "./scripts/demo_stage5_minimal.emu"
