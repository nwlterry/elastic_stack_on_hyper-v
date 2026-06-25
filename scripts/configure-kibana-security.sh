#!/usr/bin/env bash
# Generate and persist Kibana saved-object encryption key in kibana.yml.
set -euo pipefail

KIBANA_YML="/etc/kibana/kibana.yml"
KEY_FILE="/etc/kibana/.saved_objects_encryption_key"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$KIBANA_YML" ]] || { echo "Missing $KIBANA_YML" >&2; exit 1; }

if [[ -f "$KEY_FILE" ]]; then
  KEY="$(tr -d '\n' < "$KEY_FILE")"
  echo "Using existing saved-object encryption key from ${KEY_FILE}"
else
  if command -v /usr/share/kibana/bin/kibana-encryption-keys &>/dev/null; then
    KEY="$(/usr/share/kibana/bin/kibana-encryption-keys generate 2>/dev/null \
      | awk -F': ' '/xpack.encryptedSavedObjects.encryptionKey/ {print $2; exit}' \
      | tr -d ' "' || true)"
  fi
  if [[ -z "${KEY:-}" || ${#KEY} -lt 32 ]]; then
    KEY="$(openssl rand -base64 32 | tr -d '\n=/+' | head -c 32)"
  fi
  install -m 600 -o root -g root /dev/null "$KEY_FILE"
  printf '%s' "$KEY" > "$KEY_FILE"
  echo "Generated new saved-object encryption key in ${KEY_FILE}"
fi

# Flat key (supported in 8.x).
if grep -q '^xpack\.encryptedSavedObjects\.encryptionKey:' "$KIBANA_YML"; then
  sed -i "s|^xpack\.encryptedSavedObjects\.encryptionKey:.*|xpack.encryptedSavedObjects.encryptionKey: \"${KEY}\"|" "$KIBANA_YML"
else
  printf '\nxpack.encryptedSavedObjects.encryptionKey: "%s"\n' "$KEY" >> "$KIBANA_YML"
fi

if getent group kibana &>/dev/null; then
  chown root:kibana "$KIBANA_YML" "$KEY_FILE" 2>/dev/null || chown root:root "$KIBANA_YML" "$KEY_FILE"
else
  chown root:root "$KIBANA_YML" "$KEY_FILE"
fi
chmod 660 "$KIBANA_YML"
chmod 600 "$KEY_FILE"
echo "Saved-object encryption key configured in ${KIBANA_YML}"