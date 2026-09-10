#!/usr/bin/env python3
"""Remount NFS snapshot export on ES nodes that lost /mnt/es-snapshots."""
from deploy_ordered_stack import NODES, connect, run

NFS = "ismelkkbnnode01.ocplab.net:/exports/elasticsearch-snapshots"
MNT = "/mnt/es-snapshots"
FSTAB = f"{NFS} {MNT} nfs rw,soft,timeo=30,retrans=3,_netdev 0 0"
CMD = (
    "mkdir -p /mnt/es-snapshots; "
    "rpm -q nfs-utils >/dev/null || dnf install -y nfs-utils; "
    "grep -qF '/mnt/es-snapshots' /etc/fstab || "
    f"echo '{FSTAB}' >> /etc/fstab; "
    "mount -a; "
    "mount | grep snapshots || echo NO_MOUNT; "
    "su -s /bin/bash elasticsearch -c "
    "'touch /mnt/es-snapshots/.es-write-test && rm -f /mnt/es-snapshots/.es-write-test' "
    "&& echo ES_WRITE_OK || echo ES_WRITE_FAIL"
)


def main() -> int:
    rc = 0
    for key in ("es01", "es02", "es03", "es04"):
        if key not in NODES:
            continue
        c = connect(NODES[key][0])
        print(f"===== remount {key} =====", flush=True)
        out = run(c, CMD, check=False, timeout=180)
        print(out, flush=True)
        if "ES_WRITE_OK" not in out:
            rc = 1
        c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
