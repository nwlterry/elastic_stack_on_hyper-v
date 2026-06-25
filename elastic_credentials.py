#!/usr/bin/env python3
"""Read and store the elastic superuser password without accidental resets."""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent
LOCAL_PASSWORD_FILE = ROOT / "secrets" / "elastic-password"
REMOTE_PASSWORD_FILE = "/root/.elastic-stack/elastic-password"
REMOTE_PASSWORD_DIR = "/root/.elastic-stack"

RunFn = Callable[[object, str, bool, int], str]


def load_config_password() -> str | None:
    """Optional ElasticPassword from config.psd1 (never committed)."""
    for path in (ROOT / "config.psd1", ROOT / "config.psd1.example"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(r"ElasticPassword\s*=\s*'([^']+)'", text)
        if m and m.group(1) not in ("", "CHANGE_ME_elastic_password"):
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


def verify_elastic_password(c, run: RunFn, password: str) -> bool:
    auth = shlex.quote(f"elastic:{password}")
    out = run(
        c,
        f"curl -sk -o /dev/null -w '%{{http_code}}' -u {auth} "
        f"https://localhost:9200/_cluster/health 2>/dev/null",
        check=False,
        timeout=30,
    ).strip()
    return out.endswith("200")


_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_+\-=./*]+$")


def _sanitize_password(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().splitlines()[0].strip()
    if not cleaned or len(cleaned) > 128:
        return None
    if not _PASSWORD_RE.match(cleaned):
        return None
    return cleaned


def collect_password_candidates(limit: int = 30) -> list[str]:
    """Recent terminal history only — never scans grok session JSON dumps."""
    seen: set[str] = set()
    candidates: list[str] = []

    def add(value: str | None) -> None:
        cleaned = _sanitize_password(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            candidates.append(cleaned)

    term_dir = Path.home() / "terminals"
    if term_dir.is_dir():
        files = sorted(term_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if len(candidates) >= limit:
                break
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pat in (r"New value:\s*(\S+)", r"elastic=(\S+)"):
                for m in re.finditer(pat, text):
                    add(m.group(1))
                    if len(candidates) >= limit:
                        break
    return candidates


def get_elastic_password(c, run: RunFn) -> str:
    """
    Return the current elastic password without resetting it.

    Lookup order: config.psd1 ElasticPassword -> local secrets file ->
    ES01 /root/.elastic-stack/elastic-password -> verify cached candidates.
    """
    for value in (
        load_config_password(),
        load_local_password(),
        read_remote_password(c, run),
    ):
        value = _sanitize_password(value)
        if value and verify_elastic_password(c, run, value):
            save_local_password(value)
            save_remote_password(c, run, value)
            return value

    for value in collect_password_candidates():
        if verify_elastic_password(c, run, value):
            save_local_password(value)
            save_remote_password(c, run, value)
            return value

    raise RuntimeError(
        "Could not find a working elastic password without resetting.\n"
        "Set ElasticPassword in config.psd1, save it to secrets/elastic-password,\n"
        "or run: python reset_elastic_password.py  (one-time reset + save)"
    )


def reset_elastic_password(c, run: RunFn) -> str:
    """Reset elastic password once, persist locally and on ES01."""
    for attempt in range(20):
        out = run(
            c,
            "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1",
            check=False,
            timeout=120,
        )
        m = re.search(r"New (?:password|value):\s*(\S+)", out)
        if m:
            password = m.group(1)
            save_local_password(password)
            save_remote_password(c, run, password)
            if verify_elastic_password(c, run, password):
                return password
        time.sleep(12)
    raise RuntimeError("elasticsearch-reset-password failed after retries")