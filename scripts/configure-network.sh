#!/usr/bin/env bash
# Configure static IP and hostname on RHEL 8.9 VMs (NetworkManager).
set -euo pipefail

HOSTNAME=""
IP_ADDR=""
GATEWAY=""
DNS_SERVERS=""
IFACE=""

usage() {
  cat <<EOF
Usage: $0 --hostname NAME --ip CIDR --gateway GW --dns "DNS1,DNS2" [--iface IFACE]

Example:
  $0 --hostname es-node01 --ip 192.168.100.11/24 --gateway 192.168.100.1 --dns "192.168.100.1,8.8.8.8"
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME="$2"; shift 2 ;;
    --ip) IP_ADDR="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --dns) DNS_SERVERS="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -n "$HOSTNAME" && -n "$IP_ADDR" && -n "$GATEWAY" && -n "$DNS_SERVERS" ]] || usage

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

if [[ -z "$IFACE" ]]; then
  IFACE=$(nmcli -t -f DEVICE,STATE device status | awk -F: '$2=="connected"{print $1; exit}')
fi

if [[ -z "$IFACE" ]]; then
  echo "No connected interface found." >&2
  exit 1
fi

hostnamectl set-hostname "${HOSTNAME}.lab.local"
CON_NAME=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v dev="$IFACE" '$2==dev{print $1; exit}')

nmcli connection modify "$CON_NAME" \
  ipv4.method manual \
  ipv4.addresses "$IP_ADDR" \
  ipv4.gateway "$GATEWAY" \
  ipv4.dns "$DNS_SERVERS" \
  ipv6.method ignore

nmcli connection down "$CON_NAME" || true
nmcli connection up "$CON_NAME"

echo "Network configured:"
hostname -f
ip -4 addr show "$IFACE"