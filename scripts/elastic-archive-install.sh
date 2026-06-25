#!/usr/bin/env bash
# Install Elastic Agent from tar.gz archive (artifacts.elastic.co layout).
# Offline: /opt/elastic-setup/archives/elastic-agent-{version}-linux-x86_64.tar.gz
# Online fallback downloads from artifacts.elastic.co for future upgrades.
set -euo pipefail

ELASTIC_ARCHIVE_DIR="${ELASTIC_ARCHIVE_DIR:-/opt/elastic-setup/archives}"

elastic_agent_archive_name() {
  local version="$1"
  echo "elastic-agent-${version}-linux-x86_64.tar.gz"
}

elastic_agent_extract_dir() {
  local version="$1"
  echo "${ELASTIC_ARCHIVE_DIR}/elastic-agent-${version}-linux-x86_64"
}

remove_elastic_agent() {
  local version="${1:-}"
  systemctl stop elastic-agent 2>/dev/null || true
  systemctl disable elastic-agent 2>/dev/null || true
  rm -f /etc/systemd/system/elastic-agent.service 2>/dev/null || true
  rm -rf /etc/systemd/system/elastic-agent.service.d 2>/dev/null || true
  systemctl daemon-reload 2>/dev/null || true
  if command -v elastic-agent &>/dev/null; then
    elastic-agent uninstall --force 2>/dev/null || true
  fi
  if [[ -n "$version" ]]; then
    rpm -e "elastic-agent-${version}" 2>/dev/null || true
  fi
  pkill -9 -f '/var/lib/elastic-agent/data/elastic-agent' 2>/dev/null || true
  pkill -9 -f '/opt/Elastic/Agent' 2>/dev/null || true
  rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent
}

fetch_elastic_agent_archive() {
  local version="$1"
  local archive_name archive_path url

  archive_name="$(elastic_agent_archive_name "$version")"
  archive_path="${ELASTIC_ARCHIVE_DIR}/${archive_name}"
  mkdir -p "$ELASTIC_ARCHIVE_DIR"
  [[ -f "$archive_path" ]] && return 0

  url="https://artifacts.elastic.co/downloads/beats/elastic-agent/${archive_name}"
  echo "=== Downloading ${archive_name} from artifacts.elastic.co ===" >&2
  curl -fsSL -o "${archive_path}.part" "$url"
  mv "${archive_path}.part" "$archive_path"
}

extract_elastic_agent_archive() {
  local version="$1"
  local archive_name archive_path extract_dir

  archive_name="$(elastic_agent_archive_name "$version")"
  archive_path="${ELASTIC_ARCHIVE_DIR}/${archive_name}"
  extract_dir="$(elastic_agent_extract_dir "$version")"

  if [[ ! -f "$archive_path" ]]; then
    fetch_elastic_agent_archive "$version"
  fi

  if [[ -x "${extract_dir}/elastic-agent" ]]; then
    echo "Archive already extracted: ${extract_dir}" >&2
    return 0
  fi

  echo "=== Extracting ${archive_name} ===" >&2
  rm -rf "$extract_dir"
  tar -xzf "$archive_path" -C "$ELASTIC_ARCHIVE_DIR"
  [[ -x "${extract_dir}/elastic-agent" ]] || {
    echo "elastic-agent binary missing after extract in ${extract_dir}" >&2
    ls -la "$extract_dir" >&2 || true
    return 1
  }
}

elastic_agent_binary() {
  local version="$1"
  local extract_dir binary

  extract_elastic_agent_archive "$version"
  extract_dir="$(elastic_agent_extract_dir "$version")"
  binary="${extract_dir}/elastic-agent"
  [[ -x "$binary" ]] || { echo "elastic-agent binary missing in ${extract_dir}" >&2; return 1; }
  printf '%s\n' "$binary"
}

install_elastic_agent_from_archive() {
  local version="$1"
  local binary

  binary="$(elastic_agent_binary "$version")"
  echo "=== Elastic Agent ${version} ready from archive (${binary}) ===" >&2
}