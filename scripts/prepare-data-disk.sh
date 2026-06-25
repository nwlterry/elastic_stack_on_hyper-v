#!/usr/bin/env bash
# Format and mount the 500 GB Hyper-V data disk for Elasticsearch on RHEL 8.9.
set -euo pipefail

DATA_MOUNT="${DATA_MOUNT:-/data/elasticsearch}"
VG_NAME="es_vg"
LV_NAME="es_data"
LV_PATH="/dev/${VG_NAME}/${LV_NAME}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

# Find the secondary data disk (not OS, not OEMDRV kickstart ~64MB).
DATA_DISK=""
DATA_DISK_BYTES=0
for dev in /dev/sd? /dev/vd?; do
  [[ -b "$dev" ]] || continue
  if lsblk -no MOUNTPOINT "$dev" 2>/dev/null | grep -q '^/$'; then
    continue
  fi
  size_bytes=$(lsblk -bdno SIZE "$dev" 2>/dev/null || echo 0)
  # Ignore small virtual media (OEMDRV kickstart VHD, DVD, etc.)
  if [[ "$size_bytes" -lt 10737418240 ]]; then
    continue
  fi
  part_count=$(lsblk -no TYPE "$dev" | grep -c '^part$' || true)
  if [[ "$part_count" -eq 0 && "$size_bytes" -gt "$DATA_DISK_BYTES" ]]; then
    DATA_DISK="$dev"
    DATA_DISK_BYTES="$size_bytes"
  fi
done

if [[ -z "$DATA_DISK" ]]; then
  echo "Could not detect unpartitioned data disk. Available block devices:" >&2
  lsblk
  exit 1
fi

echo "Using data disk: ${DATA_DISK}"

if ! pvs "$DATA_DISK" &>/dev/null; then
  pvcreate -y "$DATA_DISK"
fi

if ! vgs "$VG_NAME" &>/dev/null; then
  vgcreate "$VG_NAME" "$DATA_DISK"
fi

if ! lvs "$LV_PATH" &>/dev/null; then
  lvcreate -l 100%FREE -n "$LV_NAME" "$VG_NAME"
fi

if ! blkid "$LV_PATH" | grep -q xfs; then
  mkfs.xfs -f "$LV_PATH"
fi

mkdir -p "$DATA_MOUNT"

if ! grep -q "$LV_PATH" /etc/fstab; then
  uuid=$(blkid -s UUID -o value "$LV_PATH")
  echo "UUID=${uuid} ${DATA_MOUNT} xfs defaults,noatime 0 0" >> /etc/fstab
fi

mount -a

# Elasticsearch runs as elasticsearch user (created during RPM install).
# Pre-create mount point ownership will be fixed by install-elasticsearch.sh.
chmod 755 "$DATA_MOUNT"

echo "Data disk ready:"
df -h "$DATA_MOUNT"
lsblk "$DATA_DISK"