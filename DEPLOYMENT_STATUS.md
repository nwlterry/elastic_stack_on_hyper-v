# Deployment Status — ism-elk-cluster (2026-06-25)

## Stack overview

| Component | FQDN | IP | Status |
|-----------|------|-----|--------|
| Elasticsearch ×3 | ismelkesnode01–03.ocplab.net | 10.44.40.31–33 | Green, 3 data nodes |
| Kibana | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Up, HTTP 200 |
| Fleet Server | ismelkflnode01.ocplab.net | 10.44.40.42 | HEALTHY, port 8220 |
| Elastic Agents | ES×3 + Kibana | — | 5 enrolled, all healthy |

- **Cluster:** `ism-elk-cluster`
- **Version:** Elasticsearch / Kibana / Elastic Agent **8.18.4**
- **Install method:** RPM (ES/Kibana), **tar.gz archive** (Fleet Server + agents)

## Access URLs

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet Server | https://ismelkflnode01.ocplab.net:8220 |

Elastic superuser password is reset by several helper scripts — use `show_elastic_password.py` or `elasticsearch-reset-password` on ES01 to obtain the current value. **Do not commit passwords to git.**

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

Bundled packages on Kibana include `fleet_server-1.6.0.zip`, `elastic_agent-2.3.0.zip`.

## Fleet policy IDs (stable)

| Policy | ID |
|--------|-----|
| Fleet Server | `9be39452-a297-4b8b-9fae-b12ab3cb9315` |
| ES agents | `f9b17f0b-f0d4-42ad-8761-2bdec42f4588` |
| Kibana agent | `3b226858-3140-4a6b-b044-05dc7819a338` |
| fleet_server package policy | `50a076fc-cfd6-48ab-b478-5f3ca207c400` |

## Successful deploy sequence (last full run)

1. **`deploy_local_epr.py`** — local EPR + air-gap Kibana + fleet_server integration
2. **`redeploy_fleet_only.py`** — Fleet Server archive install with custom CA → enrolled on 8220
3. **`resume_agent_deploy.py`** — agent policies + deploy agents on ES×3 + Kibana with custom CA

Verification: **`fleet_ps.py`** + **`verify_kibana.py`** (exit 0).

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

## Known gaps

Monitoring integrations require packages not in local EPR:

- `system@1.60.0`
- `elasticsearch@1.12.0`
- `kibana@1.11.0`

Agents enroll successfully; stack monitoring integrations cannot be added until those zips are bundled.

## Safe verification commands

```powershell
cd C:\path\to\elastic_stack_on_hyper-v
Copy-Item config.psd1.example config.psd1   # first time only; edit secrets
python run_with_pass.py fleet_ps.py
python run_with_pass.py verify_kibana.py
python show_elastic_password.py              # probe cached passwords, no reset
```