#!/usr/bin/env bash
# Install nfs-utils from attached RHEL DVD (sr0) when subscription repos are unavailable.
set -euo pipefail

if rpm -q nfs-utils &>/dev/null; then
  echo "nfs-utils already installed"
  rpm -q nfs-utils
  exit 0
fi

ISO_MNT="${ISO_MNT:-/mnt/rhel-dvd}"
mkdir -p "$ISO_MNT"

# Prefer sr0/sr1 optical media
mounted=0
for dev in /dev/sr0 /dev/sr1 /dev/cdrom; do
  [[ -b "$dev" ]] || continue
  if mount -o ro "$dev" "$ISO_MNT" 2>/dev/null; then
    echo "Mounted $dev at $ISO_MNT"
    mounted=1
    break
  fi
done

if [[ "$mounted" -ne 1 ]]; then
  echo "FAIL: could not mount RHEL ISO (sr0). Attach DVD or stage nfs-utils RPM." >&2
  lsblk
  exit 1
fi

cleanup() {
  umount "$ISO_MNT" 2>/dev/null || true
}
trap cleanup EXIT

if [[ ! -d "$ISO_MNT/BaseOS" ]]; then
  echo "FAIL: $ISO_MNT/BaseOS missing — not a RHEL install media?" >&2
  ls "$ISO_MNT" | head
  exit 1
fi

cat > /etc/yum.repos.d/rhel-dvd-local.repo <<EOF
[rhel-dvd-baseos]
name=RHEL DVD BaseOS
baseurl=file://${ISO_MNT}/BaseOS
enabled=1
gpgcheck=0

[rhel-dvd-appstream]
name=RHEL DVD AppStream
baseurl=file://${ISO_MNT}/AppStream
enabled=1
gpgcheck=0
EOF

dnf install -y --disablerepo='*' --enablerepo=rhel-dvd-baseos,rhel-dvd-appstream nfs-utils
rpm -q nfs-utils
echo "OK nfs-utils installed from DVD"
