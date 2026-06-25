#!/usr/bin/env bash
# Install Elastic RPM from /opt/elastic-setup/rpms when VMs have no outbound internet.
set -euo pipefail

elastic_rpm_dir() {
  echo "${ELASTIC_RPM_DIR:-/opt/elastic-setup/rpms}"
}

import_elastic_gpg() {
  local rpm_dir
  rpm_dir="$(elastic_rpm_dir)"
  [[ -f "${rpm_dir}/GPG-KEY-elasticsearch" ]] || return 1
  rpm --import "${rpm_dir}/GPG-KEY-elasticsearch"
}

install_elastic_rpm_local() {
  local pkg="$1"
  local version="$2"
  local rpm_dir rpm_file

  rpm_dir="$(elastic_rpm_dir)"
  rpm_file="${rpm_dir}/${pkg}-${version}-x86_64.rpm"
  [[ -f "$rpm_file" ]] || return 1

  echo "=== Installing ${pkg}-${version} from local RPM ==="
  import_elastic_gpg
  if rpm -q "${pkg}-${version}" &>/dev/null; then
    echo "${pkg}-${version} already installed"
    return 0
  fi
  dnf install -y "$rpm_file"
}