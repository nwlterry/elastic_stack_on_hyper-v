#!/usr/bin/env python3
"""Run a script with SSH_PASS from config.psd1."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
cfg = (ROOT / "config.psd1").read_text()
m = re.search(r"RootPassword\s*=\s*'([^']+)'", cfg)
if not m:
    sys.exit("Could not read RootPassword from config.psd1")
os.environ["SSH_PASS"] = m.group(1)
target = sys.argv[1] if len(sys.argv) > 1 else "status.py"
subprocess.run([sys.executable, str(ROOT / target), *sys.argv[2:]], check=False)