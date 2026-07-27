#!/usr/bin/env bash
# On each Elasticsearch node: align elasticsearch UID/GID, mount NFS, set path.repo.
set -euo pipefail

ES_UID="${ES_UID:-994}"
ES_GID="${ES_GID:-991}"
NFS_SERVER="${NFS_SERVER:-ismelkkbnnode01.ocplab.net}"
NFS_EXPORT="${NFS_EXPORT:-/exports/elasticsearch-snapshots}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/es-snapshots}"
ES_YML="${ES_YML:-/etc/elasticsearch/elasticsearch.yml}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "=== Align elasticsearch UID/GID to ${ES_UID}:${ES_GID} ==="
gid_owner=$(getent group "$ES_GID" | cut -d: -f1 || true)
if [[ -n "$gid_owner" && "$gid_owner" != "elasticsearch" ]]; then
  groupmod -n elasticsearch "$gid_owner"
elif ! getent group elasticsearch &>/dev/null; then
  groupadd -g "$ES_GID" elasticsearch
else
  cur_gid=$(getent group elasticsearch | cut -d: -f3)
  if [[ "$cur_gid" != "$ES_GID" ]]; then
    groupmod -g "$ES_GID" elasticsearch
  fi
fi

uid_owner=$(getent passwd "$ES_UID" | cut -d: -f1 || true)
if [[ -n "$uid_owner" && "$uid_owner" != "elasticsearch" ]]; then
  systemctl stop elasticsearch 2>/dev/null || true
  usermod -l elasticsearch "$uid_owner"
  usermod -g "$ES_GID" elasticsearch
elif ! id elasticsearch &>/dev/null; then
  useradd -r -u "$ES_UID" -g elasticsearch -s /sbin/nologin -d /nonexistent elasticsearch
else
  cur_uid=$(id -u elasticsearch)
  if [[ "$cur_uid" != "$ES_UID" ]]; then
    echo "  usermod elasticsearch ${cur_uid} -> ${ES_UID}"
    systemctl stop elasticsearch 2>/dev/null || true
    usermod -u "$ES_UID" elasticsearch
    for p in /data/elasticsearch /var/lib/elasticsearch /var/log/elasticsearch \
             /etc/elasticsearch /usr/share/elasticsearch; do
      [[ -e "$p" ]] && chown -R elasticsearch:elasticsearch "$p" || true
    done
  fi
  usermod -g "$ES_GID" elasticsearch 2>/dev/null || true
fi
echo "  $(id elasticsearch)"

echo "=== Install nfs-utils and mount ${NFS_SERVER}:${NFS_EXPORT} ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! rpm -q nfs-utils &>/dev/null; then
  if [[ -x "${SCRIPT_DIR}/install-nfs-from-iso.sh" ]]; then
    bash "${SCRIPT_DIR}/install-nfs-from-iso.sh"
  else
    dnf install -y nfs-utils
  fi
fi
mkdir -p "$MOUNT_POINT"
FSTAB_LINE="${NFS_SERVER}:${NFS_EXPORT} ${MOUNT_POINT} nfs rw,soft,timeo=30,retrans=3,_netdev 0 0"
if grep -qF "$MOUNT_POINT" /etc/fstab; then
  sed -i "\| ${MOUNT_POINT} |d" /etc/fstab
fi
echo "$FSTAB_LINE" >> /etc/fstab
mount -a
# Prove write as elasticsearch
su -s /bin/bash elasticsearch -c "touch ${MOUNT_POINT}/.es-write-test && rm -f ${MOUNT_POINT}/.es-write-test"
df -h "$MOUNT_POINT"

echo "=== path.repo in elasticsearch.yml ==="
cp -a "$ES_YML" "${ES_YML}.bak.$(date +%Y%m%d%H%M%S)"
# Remove existing path.repo lines / blocks
python3 - <<'PY'
import re
from pathlib import Path
path = Path("/etc/elasticsearch/elasticsearch.yml")
text = path.read_text()
# Drop single-line path.repo and multi-line list forms
lines = text.splitlines()
out = []
skip_list = False
for line in lines:
    if re.match(r"^path\.repo\s*:", line):
        # if list on same line or starts list
        if "[" in line and "]" in line:
            continue
        if line.rstrip().endswith(":") or line.rstrip().endswith("["):
            skip_list = True
            continue
        continue
    if skip_list:
        if re.match(r"^\s*-\s+", line) or re.match(r"^\s*\]", line):
            if "]" in line:
                skip_list = False
            continue
        skip_list = False
    out.append(line)
text = "\n".join(out).rstrip() + "\n"
text += "path.repo: [\"/mnt/es-snapshots\"]\n"
path.write_text(text)
print("path.repo set to [\"/mnt/es-snapshots\"]")
PY

grep -E '^path\.repo' "$ES_YML" || true
echo "Done. Restart elasticsearch for path.repo to take effect."
