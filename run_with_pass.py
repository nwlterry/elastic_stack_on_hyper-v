#!/usr/bin/env python3
"""Run a deploy script with SSH_PASS from config.psd1 (runs init wizard on first use)."""
import os
import subprocess
import sys
from pathlib import Path

from config_loader import build_deploy_context, ensure_config

ROOT = Path(__file__).parent
cfg = ensure_config()
ctx = build_deploy_context(cfg)
os.environ["SSH_PASS"] = ctx["PASSWORD"]

target = sys.argv[1] if len(sys.argv) > 1 else "status.py"
subprocess.run([sys.executable, str(ROOT / target), *sys.argv[2:]], check=False)