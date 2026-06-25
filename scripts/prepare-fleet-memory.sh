#!/usr/bin/env bash
# Add swap for fleet-server enrollment on memory-limited VMs (4-8 GB).
set -euo pipefail
SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_GB="${SWAP_GB:-8}"

if swapon --show | grep -q "$SWAPFILE"; then
  echo "Swap already active: $SWAPFILE"
  swapon --show
  exit 0
fi

if [[ ! -f "$SWAPFILE" ]]; then
  echo "Creating ${SWAP_GB}G swap at ${SWAPFILE}"
  fallocate -l "${SWAP_GB}G" "$SWAPFILE" || dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((SWAP_GB * 1024))
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE"
fi
swapon "$SWAPFILE"
grep -q "$SWAPFILE" /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
echo "Swap enabled:"
swapon --show
free -h