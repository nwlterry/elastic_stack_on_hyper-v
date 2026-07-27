#!/usr/bin/env bash
# On Kibana node: format NFS data disk (if present), export for ES snapshots.
# Ownership uses numeric ES_UID:ES_GID (994:991) so NFS all_squash maps to
# elasticsearch on ES nodes. Do NOT rename kibana user/group on this host.
set -euo pipefail

ES_UID="${ES_UID:-994}"
ES_GID="${ES_GID:-991}"
EXPORT_ROOT="${EXPORT_ROOT:-/exports/elasticsearch-snapshots}"
EXPORT_CLIENTS="${EXPORT_CLIENTS:-10.44.40.0/24}"
VG_NAME="${VG_NAME:-nfs_vg}"
LV_NAME="${LV_NAME:-es_snapshots}"
LV_PATH="/dev/${VG_NAME}/${LV_NAME}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "=== Numeric ownership for NFS map ${ES_UID}:${ES_GID} ==="
echo "  passwd $(getent passwd "$ES_UID" || echo missing)"
echo "  group  $(getent group "$ES_GID" || echo missing)"

echo "=== Detect unpartitioned data disk (>= 10G, not root) ==="
DATA_DISK=""
DATA_DISK_BYTES=0
for dev in /dev/sd? /dev/vd?; do
  [[ -b "$dev" ]] || continue
  if lsblk -no MOUNTPOINT "$dev" 2>/dev/null | grep -q '^/$'; then
    continue
  fi
  if lsblk -no MOUNTPOINT "$dev" 2>/dev/null | grep -q '/'; then
    continue
  fi
  size_bytes=$(lsblk -bdno SIZE "$dev" 2>/dev/null || echo 0)
  if [[ "$size_bytes" -lt 10737418240 ]]; then
    continue
  fi
  if [[ "$size_bytes" -gt "$DATA_DISK_BYTES" ]]; then
    DATA_DISK="$dev"
    DATA_DISK_BYTES="$size_bytes"
  fi
done

USE_ROOT_FALLBACK=0
if [[ -z "$DATA_DISK" ]] && ! vgs "$VG_NAME" &>/dev/null; then
  echo "WARN: no unpartitioned data disk yet — using ${EXPORT_ROOT} on root FS" >&2
  lsblk || true
  USE_ROOT_FALLBACK=1
fi

if [[ "$USE_ROOT_FALLBACK" -eq 0 ]]; then
  if ! vgs "$VG_NAME" &>/dev/null; then
    echo "Using data disk: ${DATA_DISK}"
    if ! pvs "$DATA_DISK" &>/dev/null; then
      pvcreate -y "$DATA_DISK"
    fi
    vgcreate "$VG_NAME" "$DATA_DISK"
  fi
  if ! lvs "$LV_PATH" &>/dev/null; then
    lvcreate -l 100%FREE -n "$LV_NAME" "$VG_NAME"
  fi
  if ! blkid "$LV_PATH" 2>/dev/null | grep -q xfs; then
    mkfs.xfs -f "$LV_PATH"
  fi
  mkdir -p "$EXPORT_ROOT"
  if ! grep -q "$EXPORT_ROOT" /etc/fstab; then
    uuid=$(blkid -s UUID -o value "$LV_PATH")
    echo "UUID=${uuid} ${EXPORT_ROOT} xfs defaults,noatime 0 0" >> /etc/fstab
  fi
  mount -a
else
  mkdir -p "$EXPORT_ROOT"
fi

chown "${ES_UID}:${ES_GID}" "$EXPORT_ROOT"
chmod 775 "$EXPORT_ROOT"

echo "=== Install nfs-utils and export ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! rpm -q nfs-utils &>/dev/null; then
  if [[ -x "${SCRIPT_DIR}/install-nfs-from-iso.sh" ]]; then
    bash "${SCRIPT_DIR}/install-nfs-from-iso.sh"
  else
    dnf install -y nfs-utils
  fi
fi

EXPORT_LINE="${EXPORT_ROOT} ${EXPORT_CLIENTS}(rw,sync,no_subtree_check,all_squash,anonuid=${ES_UID},anongid=${ES_GID})"
if [[ -f /etc/exports ]] && grep -qF "$EXPORT_ROOT" /etc/exports; then
  sed -i "\|^${EXPORT_ROOT} |d" /etc/exports
fi
echo "$EXPORT_LINE" >> /etc/exports

if systemctl list-unit-files firewalld.service &>/dev/null; then
  systemctl enable --now firewalld 2>/dev/null || true
  firewall-cmd --permanent --add-service=nfs || true
  firewall-cmd --permanent --add-service=rpc-bind || true
  firewall-cmd --permanent --add-service=mountd || true
  firewall-cmd --reload || true
fi

systemctl enable --now nfs-server rpcbind
exportfs -ra
exportfs -v

echo "=== NFS export ready ==="
df -h "$EXPORT_ROOT"
ls -ldn "$EXPORT_ROOT"
