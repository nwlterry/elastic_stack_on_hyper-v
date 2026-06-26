#!/usr/bin/env python3
"""Build Fleet package-policy stream definitions with required default vars."""
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

ROOT = Path(__file__).parent
EPR_DIR = ROOT / "packages" / "epr"

SYSTEM_METRICS = [
    "system.core",
    "system.cpu",
    "system.diskio",
    "system.filesystem",
    "system.fsstat",
    "system.load",
    "system.memory",
    "system.network",
    "system.process",
    "system.process.summary",
    "system.socket_summary",
    "system.uptime",
]

ES_METRICS = [
    "elasticsearch.stack_monitoring.cluster_stats",
    "elasticsearch.stack_monitoring.enrich",
    "elasticsearch.stack_monitoring.index",
    "elasticsearch.stack_monitoring.index_recovery",
    "elasticsearch.stack_monitoring.index_summary",
    "elasticsearch.ingest_pipeline",
    "elasticsearch.stack_monitoring.ml_job",
    "elasticsearch.stack_monitoring.node",
    "elasticsearch.stack_monitoring.node_stats",
    "elasticsearch.stack_monitoring.pending_tasks",
    "elasticsearch.stack_monitoring.shard",
]

KIBANA_METRICS = [
    "kibana.stack_monitoring.cluster_actions",
    "kibana.stack_monitoring.cluster_rules",
    "kibana.stack_monitoring.node_actions",
    "kibana.stack_monitoring.node_rules",
    "kibana.stack_monitoring.stats",
    "kibana.stack_monitoring.status",
]

KIBANA_LOGS = ["kibana.log", "kibana.audit"]

PKG_VERSIONS = {"system": "1.60.0", "elasticsearch": "1.12.0", "kibana": "2.3.1"}


def _find_manifest_path(zf, pkg, version, dataset):
    prefix = f"{pkg}-{version}/data_stream/"
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith("/manifest.yml"):
            continue
        parsed = yaml.safe_load(zf.read(name).decode())
        if parsed.get("dataset") == dataset or (
            not parsed.get("dataset") and name == f"{prefix}{dataset.split('.', 1)[-1]}/manifest.yml"
        ):
            return name
    return None


def _load_stream_defaults(pkg, version, dataset):
    zip_path = EPR_DIR / f"{pkg}-{version}.zip"
    defaults = {"period": "10s", "preserve_original_event": False}
    if not zip_path.is_file() or yaml is None:
        return defaults
    with zipfile.ZipFile(zip_path) as zf:
        manifest_path = _find_manifest_path(zf, pkg, version, dataset)
        if not manifest_path:
            return defaults
        parsed = yaml.safe_load(zf.read(manifest_path).decode())
    for stream in parsed.get("streams", []):
        for var in stream.get("vars", []):
            if var.get("required") and "default" in var:
                defaults[var["name"]] = var["default"]
    return defaults


def _var_entry(value, vtype=None):
    if isinstance(value, bool):
        return {"value": value, "type": "bool"}
    if isinstance(value, int):
        return {"value": value, "type": "integer"}
    if isinstance(value, list):
        return {"value": value, "type": vtype or "text"}
    if isinstance(value, str) and "\n" in value:
        return {"value": value, "type": vtype or "yaml"}
    return {"value": value, "type": vtype or "text"}


def build_stream(dataset, stype, extra_vars=None):
    pkg = dataset.split(".", 1)[0]
    ver = PKG_VERSIONS[pkg]
    defaults = _load_stream_defaults(pkg, ver, dataset)
    if extra_vars:
        defaults.update(extra_vars)
    vars_body = {}
    for key, value in defaults.items():
        vtype = None
        if key == "processors":
            vtype = "yaml"
        vars_body[key] = _var_entry(value, vtype)
    return {
        "id": dataset,
        "enabled": True,
        "data_stream": {"type": stype, "dataset": dataset},
        "vars": vars_body,
    }


def system_inputs():
    return [
        {
            "type": "system/metrics",
            "policy_template": "metrics",
            "enabled": True,
            "streams": [build_stream(s, "metrics") for s in SYSTEM_METRICS],
        },
        {
            "type": "logfile",
            "policy_template": "logs",
            "enabled": True,
            "streams": [
                build_stream("system.syslog", "logs", {"paths": ["/var/log/messages"]}),
                build_stream("system.auth", "logs", {"paths": ["/var/log/secure"]}),
            ],
        },
    ]


def elasticsearch_input():
    return {
        "type": "elasticsearch/metrics",
        "policy_template": "elasticsearch",
        "enabled": True,
        "streams": [build_stream(s, "metrics") for s in ES_METRICS],
    }


def kibana_inputs():
    return [
        {
            "type": "kibana/metrics",
            "policy_template": "kibana",
            "enabled": True,
            "streams": [build_stream(s, "metrics") for s in KIBANA_METRICS],
        },
        {
            "type": "logfile",
            "policy_template": "kibana",
            "enabled": True,
            "streams": [build_stream(s, "logs") for s in KIBANA_LOGS],
        },
    ]