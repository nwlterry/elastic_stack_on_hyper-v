#!/usr/bin/env python3
"""
1) Create Observability + APM sample alert rules (upgrade testing)
2) Delete ES snap pre-upgrade-apm-alert-8.18.4-20260724
3) Create full-feature system snapshot pre-upgrade-system-8.18.4-with-apm-20260724
4) Hyper-V checkpoint (all stack VMs) pre-upgrade-system-8.18.4-with-apm
"""
from __future__ import annotations

import ctypes
import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "obs_apm_alerts_and_snaps.log"
REPO = "fs_nfs_snapshots"
OLD_ES_SNAP = "pre-upgrade-apm-alert-8.18.4-20260724"
NEW_ES_SNAP = "pre-upgrade-system-8.18.4-with-apm-20260724"
HV_SNAP = "pre-upgrade-system-8.18.4-with-apm"
HV_PS1 = ROOT / "Checkpoint-AllStackVMs.ps1"
HV_LOG = ROOT / "logs" / "hv-pre-upgrade-8184-with-apm.log"

# Broad feature list (non-empty states still recorded for active features)
FEATURES = [
    "security",
    "kibana",
    "fleet",
    "async_search",
    "transform",
    "machine_learning",
    "watcher",
    "enrich",
    "geoip",
    "logstash_management",
    "synonyms",
    "tasks",
    "ent_search",
    "searchable_snapshots",
    "inference_plugin",
]


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def es_api(c, auth, method, path, body=None, timeout=600):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_json(out: str):
    for ch in ("{", "["):
        s = out.find(ch)
        if s >= 0:
            try:
                return json.loads(out[s:])
            except json.JSONDecodeError:
                pass
    return None


def rule_exists(kb, auth, name: str) -> dict | None:
    found = kibana_curl(
        kb, auth, "GET", f"/api/alerting/rules/_find?per_page=100&search={name.replace(' ', '%20')}"
    )
    for r in found.get("data") or []:
        if r.get("name") == name:
            return r
    return None


def create_rule(kb, auth, body: dict) -> dict:
    name = body["name"]
    existing = rule_exists(kb, auth, name)
    if existing:
        log(f"  SKIP exists: {name} id={existing.get('id')}")
        return existing
    r = kibana_curl(kb, auth, "POST", "/api/alerting/rule", body)
    if r.get("id"):
        log(f"  OK created: {name} id={r.get('id')} type={body.get('rule_type_id')}")
        return r
    log(f"  FAIL {name}: {json.dumps(r)[:500]}")
    return r


def create_observability_and_apm_rules(kb, auth) -> list[str]:
    log("=== Create Observability + APM upgrade-testing sample rules ===")
    created: list[str] = []
    tags = ["upgrade-test", "sample", "observability"]

    # --- Observability: Custom threshold (generic) ---
    bodies = [
        {
            "name": "Obs Sample Custom Threshold (upgrade testing)",
            "tags": tags + ["custom-threshold"],
            "rule_type_id": "observability.rules.custom_threshold",
            "consumer": "logs",
            "schedule": {"interval": "5m"},
            "params": {
                "criteria": [
                    {
                        "comparator": ">",
                        "metrics": [
                            {
                                "name": "A",
                                "aggType": "count",
                            }
                        ],
                        "threshold": [1000000],
                        "timeSize": 5,
                        "timeUnit": "m",
                    }
                ],
                "alertOnNoData": False,
                "alertOnGroupDisappear": False,
                "searchConfiguration": {
                    "query": {
                        "query": "upgrade-testing-sample OR tags:upgrade-test",
                        "language": "kuery",
                    },
                    "index": "logs-*,metrics-*,apm-*,traces-apm*,logs-apm*,metrics-apm*",
                },
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        # Log threshold
        {
            "name": "Obs Sample Log Threshold (upgrade testing)",
            "tags": tags + ["logs"],
            "rule_type_id": "logs.alert.document.count",
            "consumer": "logs",
            "schedule": {"interval": "5m"},
            "params": {
                "timeSize": 5,
                "timeUnit": "m",
                "count": {"value": 1000000, "comparator": "more than"},
                "criteria": [
                    {
                        "field": "message",
                        "comparator": "matches",
                        "value": "upgrade-testing-sample*",
                    }
                ],
                "groupBy": [],
                "logView": {
                    "logViewId": "log-view-reference-0",
                    "type": "log-view-reference",
                },
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        # Metric threshold (CPU-style sample, high bar so it won't fire)
        {
            "name": "Obs Sample Metric Threshold (upgrade testing)",
            "tags": tags + ["metrics"],
            "rule_type_id": "metrics.alert.threshold",
            "consumer": "infrastructure",
            "schedule": {"interval": "5m"},
            "params": {
                "criteria": [
                    {
                        "aggType": "avg",
                        "comparator": ">",
                        "threshold": [99.9],
                        "timeSize": 5,
                        "timeUnit": "m",
                        "metric": "system.cpu.total.norm.pct",
                    }
                ],
                "sourceId": "default",
                "alertOnNoData": False,
                "alertOnGroupDisappear": False,
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        # Inventory threshold
        {
            "name": "Obs Sample Inventory Threshold (upgrade testing)",
            "tags": tags + ["inventory"],
            "rule_type_id": "metrics.alert.inventory.threshold",
            "consumer": "infrastructure",
            "schedule": {"interval": "5m"},
            "params": {
                "nodeType": "host",
                "criteria": [
                    {
                        "metric": "cpu",
                        "comparator": ">",
                        "threshold": [99.9],
                        "timeSize": 5,
                        "timeUnit": "m",
                        "customMetric": {
                            "type": "custom",
                            "id": "alert-custom-metric",
                            "field": "",
                            "aggregation": "avg",
                        },
                    }
                ],
                "sourceId": "default",
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        # --- APM rules (high thresholds / sample service so they are safe) ---
        {
            "name": "APM Sample Latency Threshold (upgrade testing)",
            "tags": ["upgrade-test", "sample", "apm"],
            "rule_type_id": "apm.transaction_duration",
            "consumer": "apm",
            "schedule": {"interval": "5m"},
            "params": {
                "serviceName": "upgrade-testing-sample",
                "transactionType": "request",
                "transactionName": "",
                "environment": "ENVIRONMENT_ALL",
                "threshold": 5000,
                "windowSize": 5,
                "windowUnit": "m",
                "aggregationType": "avg",
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        {
            "name": "APM Sample Error Count Threshold (upgrade testing)",
            "tags": ["upgrade-test", "sample", "apm"],
            "rule_type_id": "apm.error_rate",
            "consumer": "apm",
            "schedule": {"interval": "5m"},
            "params": {
                "serviceName": "upgrade-testing-sample",
                "environment": "ENVIRONMENT_ALL",
                "threshold": 1000000,
                "windowSize": 5,
                "windowUnit": "m",
                "groupBy": ["service.name"],
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        {
            "name": "APM Sample Failed Transaction Rate (upgrade testing)",
            "tags": ["upgrade-test", "sample", "apm"],
            "rule_type_id": "apm.transaction_error_rate",
            "consumer": "apm",
            "schedule": {"interval": "5m"},
            "params": {
                "serviceName": "upgrade-testing-sample",
                "transactionType": "request",
                "environment": "ENVIRONMENT_ALL",
                "threshold": 95,
                "windowSize": 5,
                "windowUnit": "m",
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
        {
            "name": "APM Sample Anomaly (upgrade testing)",
            "tags": ["upgrade-test", "sample", "apm"],
            "rule_type_id": "apm.anomaly",
            "consumer": "apm",
            "schedule": {"interval": "5m"},
            "params": {
                "serviceName": "upgrade-testing-sample",
                "transactionType": "request",
                "environment": "ENVIRONMENT_ALL",
                "windowSize": 30,
                "windowUnit": "m",
                "anomalySeverityType": "critical",
            },
            "actions": [],
            "notify_when": "onActionGroupChange",
        },
    ]

    # Alternate param shapes if first fail
    alternates: dict[str, list[dict]] = {
        "Obs Sample Log Threshold (upgrade testing)": [
            {
                "name": "Obs Sample Log Threshold (upgrade testing)",
                "tags": tags + ["logs"],
                "rule_type_id": "logs.alert.document.count",
                "consumer": "logs",
                "schedule": {"interval": "5m"},
                "params": {
                    "timeSize": 5,
                    "timeUnit": "m",
                    "count": {"value": 1000000, "comparator": "more than"},
                    "criteria": [],
                    "groupBy": [],
                },
                "actions": [],
            },
            {
                "name": "Obs Sample Log Threshold (upgrade testing)",
                "tags": tags + ["logs"],
                "rule_type_id": "logs.alert.document.count",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "timeSize": 5,
                    "timeUnit": "m",
                    "count": {"value": 1000000, "comparator": "more than"},
                    "criteria": [],
                },
                "actions": [],
            },
        ],
        "Obs Sample Custom Threshold (upgrade testing)": [
            {
                "name": "Obs Sample Custom Threshold (upgrade testing)",
                "tags": tags + ["custom-threshold"],
                "rule_type_id": "observability.rules.custom_threshold",
                "consumer": "observability",
                "schedule": {"interval": "5m"},
                "params": {
                    "criteria": [
                        {
                            "comparator": ">",
                            "metrics": [{"name": "A", "aggType": "count"}],
                            "threshold": [1000000],
                            "timeSize": 5,
                            "timeUnit": "m",
                        }
                    ],
                    "alertOnNoData": False,
                    "alertOnGroupDisappear": False,
                    "searchConfiguration": {
                        "query": {"query": "*", "language": "kuery"},
                        "index": "logs-*",
                    },
                },
                "actions": [],
            },
            {
                "name": "Obs Sample Custom Threshold (upgrade testing)",
                "tags": tags + ["custom-threshold"],
                "rule_type_id": "observability.rules.custom_threshold",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "criteria": [
                        {
                            "comparator": ">",
                            "metrics": [{"name": "A", "aggType": "count"}],
                            "threshold": [1000000],
                            "timeSize": 5,
                            "timeUnit": "m",
                        }
                    ],
                    "alertOnNoData": False,
                    "searchConfiguration": {
                        "query": {"query": "*", "language": "kuery"},
                        "index": "metrics-*",
                    },
                },
                "actions": [],
            },
        ],
        "APM Sample Latency Threshold (upgrade testing)": [
            {
                "name": "APM Sample Latency Threshold (upgrade testing)",
                "tags": ["upgrade-test", "sample", "apm"],
                "rule_type_id": "apm.transaction_duration",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "environment": "ENVIRONMENT_ALL",
                    "threshold": 5000,
                    "windowSize": 5,
                    "windowUnit": "m",
                    "aggregationType": "avg",
                },
                "actions": [],
            },
        ],
        "APM Sample Error Count Threshold (upgrade testing)": [
            {
                "name": "APM Sample Error Count Threshold (upgrade testing)",
                "tags": ["upgrade-test", "sample", "apm"],
                "rule_type_id": "apm.error_rate",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "environment": "ENVIRONMENT_ALL",
                    "threshold": 1000000,
                    "windowSize": 5,
                    "windowUnit": "m",
                },
                "actions": [],
            },
        ],
        "APM Sample Failed Transaction Rate (upgrade testing)": [
            {
                "name": "APM Sample Failed Transaction Rate (upgrade testing)",
                "tags": ["upgrade-test", "sample", "apm"],
                "rule_type_id": "apm.transaction_error_rate",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "environment": "ENVIRONMENT_ALL",
                    "threshold": 95,
                    "windowSize": 5,
                    "windowUnit": "m",
                },
                "actions": [],
            },
        ],
        "APM Sample Anomaly (upgrade testing)": [
            {
                "name": "APM Sample Anomaly (upgrade testing)",
                "tags": ["upgrade-test", "sample", "apm"],
                "rule_type_id": "apm.anomaly",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "environment": "ENVIRONMENT_ALL",
                    "windowSize": 30,
                    "windowUnit": "m",
                    "anomalySeverityType": "critical",
                },
                "actions": [],
            },
        ],
        "Obs Sample Metric Threshold (upgrade testing)": [
            {
                "name": "Obs Sample Metric Threshold (upgrade testing)",
                "tags": tags + ["metrics"],
                "rule_type_id": "metrics.alert.threshold",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "criteria": [
                        {
                            "aggType": "avg",
                            "comparator": ">",
                            "threshold": [99.9],
                            "timeSize": 5,
                            "timeUnit": "m",
                            "metric": "system.cpu.total.norm.pct",
                        }
                    ],
                    "sourceId": "default",
                    "alertOnNoData": False,
                },
                "actions": [],
            },
        ],
        "Obs Sample Inventory Threshold (upgrade testing)": [
            {
                "name": "Obs Sample Inventory Threshold (upgrade testing)",
                "tags": tags + ["inventory"],
                "rule_type_id": "metrics.alert.inventory.threshold",
                "consumer": "alerts",
                "schedule": {"interval": "5m"},
                "params": {
                    "nodeType": "host",
                    "criteria": [
                        {
                            "metric": "cpu",
                            "comparator": ">",
                            "threshold": [99.9],
                            "timeSize": 5,
                            "timeUnit": "m",
                        }
                    ],
                    "sourceId": "default",
                },
                "actions": [],
            },
        ],
    }

    for body in bodies:
        name = body["name"]
        r = create_rule(kb, auth, body)
        if r.get("id"):
            created.append(name)
            continue
        for alt in alternates.get(name, []):
            r2 = create_rule(kb, auth, alt)
            if r2.get("id"):
                created.append(name)
                break
        else:
            log(f"  WARN could not create {name}")

    # list all upgrade-test rules
    allr = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100&search=upgrade")
    names = [(x.get("name"), x.get("rule_type_id"), x.get("id")) for x in (allr.get("data") or [])]
    log(f"upgrade-test rules: {json.dumps(names, indent=2)}")
    return created


def recreate_es_snapshot(auth: str) -> str:
    log(f"=== Delete ES snap {OLD_ES_SNAP} then create {NEW_ES_SNAP} ===")
    c = connect(NODES["es02"][0], attempts=20)
    listing = es_api(
        c, auth, "GET",
        f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state",
    )
    log(listing[:800])
    print(es_api(c, auth, "DELETE", f"/_snapshot/{REPO}/{OLD_ES_SNAP}?pretty", timeout=600), flush=True)
    time.sleep(2)
    # also ensure feature list: include all available via *
    body = {
        "indices": ".*,*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": ["*"],
        "metadata": {
            "taken_by": "create_obs_apm_alerts_and_snaps",
            "taken_because": (
                "full system snapshot after Observability+APM sample alerts; "
                "all feature states + system indices + global state"
            ),
            "es_version": "8.18.4",
            "includes": "observability-alerts,apm-alerts,apm-server,upgrade-testing-sample",
        },
    }
    # try * first; if empty features on SUCCESS, recreate with explicit list
    print(
        es_api(
            c, auth, "PUT",
            f"/_snapshot/{REPO}/{NEW_ES_SNAP}?wait_for_completion=false&pretty",
            body, timeout=120,
        ),
        flush=True,
    )
    state = "IN_PROGRESS"
    feats: list = []
    for i in range(180):
        out = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{NEW_ES_SNAP}?pretty", timeout=120)
        data = parse_json(out) or {}
        snaps = data.get("snapshots") or []
        s = snaps[0] if snaps else data
        state = s.get("state") or state
        feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
        log(f"  poll {i}: state={state} shards={s.get('shards')} features={feats}")
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            # if features empty, delete and recreate with explicit FEATURES
            if state == "SUCCESS" and not feats:
                log("  features empty with *; recreating with explicit feature list")
                es_api(c, auth, "DELETE", f"/_snapshot/{REPO}/{NEW_ES_SNAP}?pretty", timeout=600)
                body2 = dict(body)
                body2["feature_states"] = FEATURES
                body2["indices"] = ".*"
                es_api(
                    c, auth, "PUT",
                    f"/_snapshot/{REPO}/{NEW_ES_SNAP}?wait_for_completion=false&pretty",
                    body2, timeout=120,
                )
                state = "IN_PROGRESS"
                continue
            log(json.dumps({
                "snapshot": s.get("snapshot"),
                "state": state,
                "include_global_state": s.get("include_global_state"),
                "indices_count": len(s.get("indices") or []),
                "feature_states": feats,
                "shards": s.get("shards"),
            }, indent=2))
            break
        time.sleep(10)
    log(es_api(
        c, auth, "GET",
        f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state",
    )[:1500])
    c.close()
    return state


def write_hv_ps1() -> None:
    HV_PS1.write_text(
        f"""#Requires -Modules Hyper-V
param(
    [string]$SnapshotName = '{HV_SNAP}',
    [string]$LogPath = '{HV_LOG.as_posix().replace('/', '\\\\')}'
)
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {{
    $line = "[{{0}}] {{1}}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}}
$vms = @(
    'ISMELKESNODE01','ISMELKESNODE02','ISMELKESNODE03','ISMELKESNODE04',
    'ISMELKKBNNODE01','ISMELKFLNODE01'
)
W "START SnapshotName=$SnapshotName (all stack VMs; keep existing checkpoints)"
foreach ($vmName in $vms) {{
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {{ W "WARN missing $vmName"; continue }}
    $existing = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    W "=== $vmName State=$($vm.State) existing=$($existing.Count) names=$($existing.Name -join ';') ==="
    if ($existing | Where-Object {{ $_.Name -eq $SnapshotName }}) {{
        W "  SKIP already has $SnapshotName"
        continue
    }}
    try {{
        W "  Checkpoint-VM -> $SnapshotName"
        Checkpoint-VM -Name $vmName -SnapshotName $SnapshotName -ErrorAction Stop
        W "  OK $vmName"
    }} catch {{
        W "  ERROR $vmName : $_"
    }}
}}
W "=== VERIFY ==="
$ok = $true
foreach ($vmName in $vms) {{
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    $has = $snaps | Where-Object {{ $_.Name -eq $SnapshotName }}
    W "$vmName count=$($snaps.Count) names=$($snaps.Name -join ', ')"
    if (-not $has) {{ $ok = $false }}
}}
if ($ok) {{ W "DONE" }} else {{ W "DONE_WITH_ERRORS"; exit 1 }}
exit 0
""",
        encoding="utf-8",
    )


def elevate_hv() -> bool:
    write_hv_ps1()
    HV_LOG.parent.mkdir(parents=True, exist_ok=True)
    HV_LOG.write_text("", encoding="utf-8")
    arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{HV_PS1}" '
        f'-SnapshotName "{HV_SNAP}" -LogPath "{HV_LOG}"'
    )
    log(f"=== Hyper-V checkpoint {HV_SNAP} ===")
    log(f"arg={arg}")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", arg, str(ROOT), 1
    )
    log(f"ShellExecuteW={rc}")
    if rc <= 32:
        return False
    for i in range(240):
        if HV_LOG.is_file():
            text = HV_LOG.read_text(encoding="utf-8", errors="replace")
            if "DONE" in text:
                log(text[-2500:])
                return "DONE_WITH_ERRORS" not in text
            if i % 6 == 0 and text.strip():
                log(f"--- hv @ {i*5}s ---\n{text[-600:]}")
        time.sleep(5)
    log("TIMEOUT Hyper-V")
    if HV_LOG.is_file():
        log(HV_LOG.read_text(encoding="utf-8", errors="replace")[-2000:])
    return False


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START obs/apm alerts + full ES snap + HV snap")

    es = connect(NODES["es02"][0], attempts=30)
    auth = curl_elastic_auth(get_elastic_password(es))
    es.close()

    kb = connect(NODES["kibana"][0], attempts=40)
    try:
        created = create_observability_and_apm_rules(kb, auth)
        log(f"created_or_existing_count={len(created)}")
    finally:
        kb.close()

    state = recreate_es_snapshot(auth)
    if state != "SUCCESS":
        log(f"ERROR ES snapshot state={state}")
        return 2

    hv_ok = elevate_hv()
    log(f"Hyper-V ok={hv_ok}")

    # final verify
    c = connect(NODES["es02"][0])
    print(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state"), flush=True)
    c.close()
    kb = connect(NODES["kibana"][0], attempts=20)
    rules = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100&search=upgrade")
    for r in rules.get("data") or []:
        log(f"rule: {r.get('name')} | {r.get('rule_type_id')} | {r.get('id')}")
    kb.close()

    log(
        f"\n=== DONE ===\n"
        f"Obs/APM sample rules created\n"
        f"Deleted ES snap: {OLD_ES_SNAP}\n"
        f"New ES snap: {NEW_ES_SNAP} state={state}\n"
        f"Hyper-V snap: {HV_SNAP} ok={hv_ok}\n"
    )
    return 0 if hv_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
