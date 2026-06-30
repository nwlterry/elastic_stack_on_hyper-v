# Deployment Status — ism-elk-cluster (2026-06-30)

## Stack overview

| Component | FQDN | IP | Status |
|-----------|------|-----|--------|
| Elasticsearch ×3 | ismelkesnode01–03.ocplab.net | 10.44.40.31–33 | Green, 3 data nodes |
| Kibana | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Up, HTTP 200 |
| Fleet Server | ismelkflnode01.ocplab.net | 10.44.40.42 | HEALTHY, port 8220 |
| Elastic Agents | ES×3 + Kibana + Fleet | — | 5 enrolled |

- **Cluster:** `ism-elk-cluster`
- **Current versions:** Elasticsearch **9.4.1**; Kibana / Fleet Server / agents **8.18.4**
- **Baseline snapshot:** `pre-upgrade-9.4.1-20260629-1535` (rollback point for all 5 VMs)
- **Install method:** RPM (ES/Kibana), **tar.gz archive** (Fleet Server + agents)
- **Pre-upgrade snapshot:** `pre-upgrade-9.4.1-20260629-1535` (all 5 VMs)

## Access URLs

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet Server | https://ismelkflnode01.ocplab.net:8220 |

Elastic superuser password is reset by several helper scripts — use `show_elastic_password.py` or `elasticsearch-reset-password` on ES01 to obtain the current value. **Do not commit passwords to git.**

## Upgrade runbook

### 1. Download packages

```powershell
python download_upgrade_packages.py
```

### 2. Create checkpoints (before any upgrade)

```powershell
.\Snapshot-ElasticVMs.ps1
```

### 3. Choose upgrade path

| Goal | Command |
|------|---------|
| Full stack (ES + Kibana + agents → 9.4.1) | `python upgrade_elastic_stack.py` |
| ES only (Kibana/Fleet stay @ 8.18.4) | `python upgrade_es_only.py` |
| Fleet rollback + artifact upgrade | `python rollback_upgrade_fleet.py` |
| Fleet rollback + reinstall fallback | `python rollback_reinstall_fleet.py` |
| Agents only (Fleet already @ target) | `python fleet_bulk_upgrade_agents.py` |

### 4. Rollback all VMs

```powershell
.\Restore-ElasticVMs.ps1 -SnapshotName pre-upgrade-9.4.1-20260629-1535
```

ES rolling order: `ismelkesnode03` (cold) → `ismelkesnode01` (warm) → `ismelkesnode02` (hot/master).

Upgrade path: **8.18.4 → 8.19.9 → 9.4.1** (Elastic requires 8.19 before 9.4).

Artifact mirror for agent upgrades: `http://10.44.40.42:8081/downloads/`

## x.509 / custom CA fix (complete)

Elasticsearch auto-configured `http_ca.crt` is used as the custom CA during elastic-agent enrollment.

**Scripts:** `scripts/elastic-agent-ca.sh`

| Flag | Used by |
|------|---------|
| `--certificate-authorities=/etc/elastic-agent/certs/http_ca.crt` | All agents |
| `--fleet-server-es-ca=...` | Fleet Server |
| `--fleet-server-es-ca-trusted-fingerprint=e8c9d21d469b064de993e40313e6f8312304356eeea9ff2d633a033b22792bd1` | Fleet Server |

**CA staged at:** `/opt/elastic-setup/certs/`, `/etc/elasticsearch/certs/`, `/etc/elastic-agent/certs/`

- Fleet Server: trusts ES via custom CA (no `--insecure` for ES)
- Regular agents: custom CA for ES + `--insecure` only for Fleet Server self-signed cert on 8220

## Air-gapped Fleet (complete)

Kibana cannot reach `epr.elastic.co`. Local package registry mock on port 8080:

- `scripts/local-epr-server.py` + `scripts/install-local-epr.sh` → systemd `local-epr.service`
- `scripts/configure-fleet-airgap.sh` → `xpack.fleet.isAirGapped: true`, `registryUrl: http://127.0.0.1:8080`
- `deploy_local_epr.py` orchestrates EPR + air-gap + fleet_server integration

Agent artifact mirror on Fleet (port 8081) for `bulk_upgrade`:

- `scripts/agent-artifact-server.py` + `agent_artifact_upgrade.py`

Bundled packages on Kibana include `fleet_server-1.6.0.zip`, `elastic_agent-2.3.0.zip`.

## Fleet policy IDs (stable)

| Policy | ID |
|--------|-----|
| Fleet Server | `9be39452-a297-4b8b-9fae-b12ab3cb9315` |
| ES agents | `f9b17f0b-f0d4-42ad-8761-2bdec42f4588` |
| Kibana agent | `3b226858-3140-4a6b-b044-05dc7819a338` |
| fleet_server package policy | `50a076fc-cfd6-48ab-b478-5f3ca207c400` |

## Scripts with safety guards (skip when healthy)

These exit immediately if Fleet is already HEALTHY — do not disrupt a working stack:

- `kill_fleet_enroll.py`
- `rerun_fleet_server.py`
- `redeploy_fleet_only.py`
- `fix_fleet_integration.py`
- `run_create_fleet_policy.py`
- `bg_create_fleet_policy.py`
- `recover_kibana.py` (skips if Kibana stable)
- `resume_agent_deploy.py` (skips if 5+ agents enrolled)

## Do NOT run (destructive / redundant)

- `kill_fleet_enroll.py` + `rerun_fleet_server.py` while Fleet is healthy
- Repeated `fix_fleet_integration.py`, `fix_airgap_fleet.py`, `run_create_fleet_policy.py` — integration already exists
- `rollback_upgrade_fleet.py` unless Fleet rollback is intentional (~20–30 min, unenrolls Fleet)

## Known gaps

Monitoring integrations require packages not in local EPR:

- `system@1.60.0`
- `elasticsearch@1.12.0`
- `kibana@1.11.0`

Agents enroll successfully; stack monitoring integrations cannot be added until those zips are bundled.

## Safe verification commands

```powershell
cd C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v
python run_with_pass.py fleet_ps.py
python run_with_pass.py verify_kibana.py
python show_elastic_password.py
```