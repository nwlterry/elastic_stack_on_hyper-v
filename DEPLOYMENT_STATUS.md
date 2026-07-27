# Deployment Status — ism-elk-cluster (2026-07-27)

## Stack overview

| Component | FQDN | IP | Status |
|-----------|------|-----|--------|
| Elasticsearch es01 | ismelkesnode01.ocplab.net | 10.44.40.31 | 8.18.4 — master, data_content |
| Elasticsearch es02 | ismelkesnode02.ocplab.net | 10.44.40.32 | 8.18.4 — master, data_content (often elected master) |
| Elasticsearch es03 | ismelkesnode03.ocplab.net | 10.44.40.33 | 8.18.4 — master, data_content, **data_hot** |
| Elasticsearch es04 | ismelkesnode04.ocplab.net | 10.44.40.34 | **8.19.18** — data_hot, ingest, remote, transform |
| Kibana | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Up |
| Fleet Server | ismelkflnode01.ocplab.net | 10.44.40.42 | Fleet + lab APM Server on :8200 |

- **Cluster:** `ism-elk-cluster` (4 ES nodes joined)
- **Health:** **yellow** — all primaries assigned; ~4 unassigned **replicas** on restricted system indices (mixed 8.18.4 / 8.19.18). See [docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md).
- **Snapshot repo:** `fs_nfs_snapshots`
- **Install method:** RPM (ES/Kibana); tar.gz (Fleet Server + agents)
- **Baseline lab path:** 8.18.4 with optional rolling ES upgrade to **8.19.18**

## Access URLs

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet Server | https://ismelkflnode01.ocplab.net:8220 |
| APM (lab) | http://ismelkflnode01.ocplab.net:8200 |

Elastic password: `secrets/elastic-password` or `python show_elastic_password.py`. **Do not commit passwords.**

## Recent lab milestones (July 2026)

1. Stripped es01–es03 data roles to **data_content** (es03 later re-added **data_hot** for tier prefs).
2. Hyper-V snaps: `pre-upgrade-system-8.18.4-*`, `pre-upgrade-system-8.18.4-with-apm`, `post-upgrade-system-8.19.18-*`.
3. Rolling ES upgrade to **8.19.18** with timed orchestrator (`upgrade_es_to_8_19_18.py`).
4. Standalone APM on Fleet host + observability/APM sample alert rules; second ES + HV snaps with APM.
5. **Downgrade experiment:** RPM reinstall 8.18.4 on es01–es03, wipe data, restore NFS snapshot — es04 **does not** rejoin if cluster UUID changes.
6. **HV restore** of es01–es03 to `pre-upgrade-system-8.18.4-with-apm` — es04 **rejoins** (mixed version).
7. Yellow mitigation: set replicas to 0 and disable `auto_expand_replicas` on non-system indices (`fix_yellow_mixed_version.py`). Residual yellow = system/restricted indices only.

## Hyper-V snapshot workflow

```powershell
# Elevated where required
.\Checkpoint-EsNodes.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
.\Restore-Es01to03-To-Snap.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
.\Remove-And-Retake-HyperVSnap.ps1 -SnapshotName post-upgrade-system-8.19.18-20260724
```

Python elevation wrappers: `run_hv_snap_elevated.py`, `run_hv_retake_post81918.py`.

## Upgrade / downgrade commands

| Goal | Command |
|------|---------|
| Download offline packages | `python download_upgrade_packages.py` |
| ES rolling upgrade → 8.19.18 | `python upgrade_es_to_8_19_18.py` |
| Per-node 8.18.4 RPM path | `scripts/downgrade-es-node-8184.sh` |
| Full downgrade + snapshot restore | `python complete_downgrade_restore.py` |
| Reduce yellow (mixed version) | `python fix_yellow_mixed_version.py` |
| APM + alert rules + snaps | `python deploy_apm_finish.py` then `python create_obs_apm_alerts_and_snaps.py` |

Full narrative: **[docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md)**  
Downgrade run report: `logs/downgrade_es01_03_procedure_report.txt`

## Path to green (mixed cluster)

1. **Preferred:** rolling-upgrade es01–es03 to 8.19.18 so replicas of 8.19 primaries can allocate.
2. **Or:** Hyper-V restore **all** ES nodes (including es04) to the same 8.18.4 snap.
3. Do **not** expect green with a single 8.19 hot node holding system-index primaries and 8.18 masters only.

## x.509 / custom CA

Elasticsearch auto-configured `http_ca.crt` is used as the custom CA during elastic-agent enrollment.

**Scripts:** `scripts/elastic-agent-ca.sh`

| Flag | Used by |
|------|---------|
| `--certificate-authorities=/etc/elastic-agent/certs/http_ca.crt` | All agents |
| `--fleet-server-es-ca=...` | Fleet Server |
| `--fleet-server-es-ca-trusted-fingerprint=e8c9d21d469b064de993e40313e6f8312304356eeea9ff2d633a033b22792bd1` | Fleet Server |

## Air-gapped Fleet

- Local EPR mock on port 8080; artifact mirror on Fleet `:8081`
- `deploy_local_epr.py`, `scripts/local-epr-server.py`, `scripts/configure-fleet-airgap.sh`

## Fleet policy IDs (stable)

| Policy | ID |
|--------|-----|
| Fleet Server | `9be39452-a297-4b8b-9fae-b12ab3cb9315` |
| ES agents | `f9b17f0b-f0d4-42ad-8761-2bdec42f4588` |
| Kibana agent | `3b226858-3140-4a6b-b044-05dc7819a338` |
| fleet_server package policy | `50a076fc-cfd6-48ab-b478-5f3ca207c400` |

## Safe verification

```powershell
cd C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v
python show_elastic_password.py
python fix_yellow_mixed_version.py
# or curl health against es01 with secrets/elastic-password
```

## Do not commit

- `config.psd1`, `secrets/*` passwords
- `packages/*.rpm` / large archives
- One-shot `_dbg_*` / `_q*` helpers (local only)
