#!/usr/bin/env python3
"""Create OEMDRV-labeled ISO with ks.cfg for unattended RHEL install."""
import io
import sys
from pathlib import Path

try:
    import pycdlib
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pycdlib", "-q"])
    import pycdlib


def main():
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <ks.cfg> <output.iso>")

    ks_data = Path(sys.argv[1]).read_bytes()
    out = Path(sys.argv[2])

    iso = pycdlib.PyCdlib()
    iso.new(joliet=3, vol_ident="OEMDRV")
    fp = io.BytesIO(ks_data)
    iso.add_fp(fp, len(ks_data), iso_path="/KS.CFG;1", joliet_path="/ks.cfg")
    iso.write(str(out))
    iso.close()
    print(f"Created {out}")


if __name__ == "__main__":
    main()