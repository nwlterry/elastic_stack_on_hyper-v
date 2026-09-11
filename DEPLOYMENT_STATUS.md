# Deployment Status — ism-elk-cluster (2026-09-11)

## Stack overview

| Component | FQDN | IP | Status |
|-----------|------|-----|--------|
| Elasticsearch es01 | ismelkesnode01.ocplab.net | 10.44.40.31 | **9.5.3** — master, data_content |
| Elasticsearch es02 | ismelkesnode02.ocplab.net | 10.44.40.32 | **9.5.3** — master, data_content |
| Elasticsearch es03 | ismelkesnode03.ocplab.net | 10.44.40.33 | **9.5.3** — master, data_content, **data_hot** (elected master after 9.5.3 roll) |
| Elasticsearch es04 | ismelkesnode04.ocplab.net | 10.44.40.34 | **9.5.3** — data_hot, ingest, remote, transform |
| Kibana | ismelkkbnnode01.ocplab.net | 10.44.40.41 | **9.5.3** — `/api/status` available |
| Fleet Server | ismelkflnode01.ocplab.net | 10.44.40.42 | **9.5.3** Fleet + lab APM Server on :8200 (VM started at 4 GB RAM) |

- **Cluster:** `ism-elk-cluster` (4 ES nodes joined)
- **Health:** **green** (4 nodes, 0 unassigned primaries) on **9.5.3**
- **Snapshot repo:** `fs_nfs_snapshots` — **only** snapshot `pre-upgrade-to-9.5.3` (SUCCESS)
- **Install method:** RPM (ES/Kibana); tar.gz (Fleet Server + agents)
- **Current lab path:** full stack **9.5.3**. Bootstrap remains 8.18.4. Procedure: `docs/LAB_OPS_9_5_3.md`.

## Access URLs

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet Server | https://ismelkflnode01.ocplab.net:8220 |
| APM (lab) | http://ismelkflnode01.ocplab.net:8200 |

Elastic password: `secrets/elastic-password` or `python show_elastic_password.py`. **Do not commit passwords.**

## Recent lab milestones (September 2026)

1. **2026-09-11:** Upgraded full stack **8.19.18 → 9.5.3**. NFS snapshots pruned to only `pre-upgrade-to-9.5.3`. Procedure and gotchas: [docs/LAB_OPS_9_5_3.md](docs/LAB_OPS_9_5_3.md).
2. **2026-09-11:** ES 9.x refuses `tracing.apm.*` in `elasticsearch.yml` — strip before start. Fleet `bulk_upgrade` to 9.x is blocked until Fleet Server is on 9.x; re-enroll Fleet Server first. Kibana 9 Fleet token API is `/api/fleet/enrollment_api_keys`.
3. **2026-09-10:** Rolling-upgraded remaining ES nodes (es01–es03) from 8.18.4 → **8.19.18**; es04 was already 8.19.18. Cluster went **green**. Kibana RPM **8.19.18**. Fleet + agents later re-enrolled at 8.19.18.
4. NFS `fs_nfs_snapshots` was disabled (`index-13` / master verify failed) because **es02 and es03 had no NFS mount**. Fix: `python remount_es_nfs.py` then `python create_named_snapshot.py --reregister --name <name>`.
5. Local RPM upgrades must use `dnf install --disablerepo='*'` — stale `rhel-dvd-local.repo` otherwise fails after ES is already stopped.
6. Offline packages for **8.19.18** and **9.5.3** in `packages/` (`python download_upgrade_packages.py`).

July 2026 history (mixed-version / downgrade / APM) remains in [docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md).

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
| Download offline packages (8.19.18 + 9.5.3) | `python download_upgrade_packages.py` |
| ES + Kibana + agents → 8.19.18 | `python upgrade_elastic_stack.py --to 8.19.18` |
| ES-only timed roll → 8.19.18 | `python upgrade_es_to_8_19_18.py` |
| Remount NFS snapshot share | `python remount_es_nfs.py` |
| NFS snapshot (no deletes) | `python create_named_snapshot.py --reregister --name <name>` |
| Keep only one NFS snapshot | `python prune_nfs_snapshots_keep.py --keep pre-upgrade-to-9.5.3` |
| 9.5.3 preflight | `python prepare_upgrade_9_5_3.py` |
| 9.5.3 roll (after Assistant) | `python upgrade_elastic_stack.py --to 9.5.3` |
| Re-enroll Fleet + agents at a version | `python reenroll_fleet_and_agents.py --version 9.5.3` |
| Per-node 8.18.4 RPM path | `scripts/downgrade-es-node-8184.sh` |
| Full downgrade + snapshot restore | `python complete_downgrade_restore.py` |
| Reduce yellow (mixed version) | `python fix_yellow_mixed_version.py` |

Full narrative: **[docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md)**  
Downgrade run report: `logs/downgrade_es01_03_procedure_report.txt`

## Path to green (mixed cluster)

Resolved **2026-09-10** by rolling es01–es03 to 8.19.18 (all four nodes same version). Historical options if mixed again:

1. **Preferred:** rolling-upgrade remaining 8.18 nodes to 8.19.18 so replicas of 8.19 primaries can allocate.
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
