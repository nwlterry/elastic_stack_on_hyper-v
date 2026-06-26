#!/usr/bin/env python3
"""Create and store the Fleet integration monitoring user (no elastic superuser reset)."""
from __future__ import annotations

import re
import secrets
import shlex
import string
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent
LOCAL_PASSWORD_FILE = ROOT / "secrets" / "monitoring-password"
REMOTE_PASSWORD_FILE = "/root/.elastic-stack/monitoring-password"
REMOTE_PASSWORD_DIR = "/root/.elastic-stack"
DEFAULT_MONITORING_USER = "elastic_monitoring"
MONITORING_ROLES = ("monitoring_user", "remote_monitoring_collector", "kibana_user")

RunFn = Callable[[object, str, bool, int], str]


def load_config_user() -> str | None:
    for path in (ROOT / "config.psd1", ROOT / "config.psd1.example"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(r"MonitoringUser\s*=\s*'([^']+)'", text)
        if m and m.group(1) not in ("", "CHANGE_ME_monitoring_user"):
            return m.group(1)
    return None


def load_config_password() -> str | None:
    for path in (ROOT / "config.psd1", ROOT / "config.psd1.example"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(r"MonitoringPassword\s*=\s*'([^']+)'", text)
        if m and m.group(1) not in ("", "CHANGE_ME_monitoring_password"):
            return m.group(1)
    return None


def load_local_password() -> str | None:
    if LOCAL_PASSWORD_FILE.is_file():
        value = LOCAL_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        return value or None
    return None


def save_local_password(password: str) -> None:
    LOCAL_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_PASSWORD_FILE.write_text(password.strip() + "\n", encoding="utf-8")


def read_remote_password(c, run: RunFn) -> str | None:
    out = run(
        c,
        f"cat {shlex.quote(REMOTE_PASSWORD_FILE)} 2>/dev/null",
        check=False,
        timeout=30,
    ).strip()
    return out or None


def save_remote_password(c, run: RunFn, password: str) -> None:
    quoted = shlex.quote(password.strip())
    run(
        c,
        f"mkdir -p {shlex.quote(REMOTE_PASSWORD_DIR)} && "
        f"printf '%s\\n' {quoted} > {shlex.quote(REMOTE_PASSWORD_FILE)} && "
        f"chmod 600 {shlex.quote(REMOTE_PASSWORD_FILE)}",
        check=False,
        timeout=30,
    )


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "_-+="
    return "".join(secrets.choice(alphabet) for _ in range(length))


def resolve_monitoring_password(c, run: RunFn) -> str:
    for value in (load_config_password(), load_local_password(), read_remote_password(c, run)):
        if value:
            return value
    password = generate_password()
    save_local_password(password)
    save_remote_password(c, run, password)
    return password


def resolve_monitoring_user() -> str:
    return load_config_user() or DEFAULT_MONITORING_USER


def ensure_monitoring_user(c, run: RunFn, elastic_pwd: str) -> tuple[str, str]:
    """Create/update monitoring user with integration roles; return (user, password)."""
    user = resolve_monitoring_user()
    password = resolve_monitoring_password(c, run)
    auth = shlex.quote(f"elastic:{elastic_pwd}")
    body = shlex.quote(
        __import__("json").dumps(
            {
                "password": password,
                "roles": list(MONITORING_ROLES),
                "full_name": "Fleet stack monitoring",
                "metadata": {"purpose": "fleet-integration-monitoring"},
            }
        )
    )
    out = run(
        c,
        f"curl -sk -u {auth} -X PUT 'https://localhost:9200/_security/user/{user}' "
        f"-H 'Content-Type: application/json' -d {body}",
        check=False,
        timeout=60,
    )
    if '"created"' not in out and '"updated"' not in out and '"acknowledged"' not in out:
        raise RuntimeError(f"Failed to create monitoring user {user}: {out[-500:]}")
    save_local_password(password)
    save_remote_password(c, run, password)
    return user, password