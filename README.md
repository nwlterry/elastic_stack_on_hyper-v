# Elastic Stack on Hyper-V (RHEL 8.10)

Automated deployment and upgrade of **Elasticsearch** (4 nodes), **Kibana**, **Fleet Server**, and **Elastic Agents** on RHEL 8.10 Hyper-V VMs with air-gapped Fleet, custom CA enrollment, NFS snapshots, APM, and lab upgrade/downgrade tooling.

| Doc | Contents |
|-----|----------|
| **[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)** | Live verified stack state, policy IDs, safe commands |
| **[docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md)** | Full 8.18.4 ↔ 8.19.18 upgrade, HV snaps, APM, rejoin, yellow health |

## Architecture

### VMs (6 total)

| VM | FQDN | IP | Role | RAM |
|----|------|-----|------|-----|
| **ISMELKESNODE01** | ismelkesnode01.ocplab.net | 10.44.40.31 | Elasticsearch es01 | 8 GB |
| **ISMELKESNODE02** | ismelkesnode02.ocplab.net | 10.44.40.32 | Elasticsearch es02 | 8 GB |
| **ISMELKESNODE03** | ismelkesnode03.ocplab.net | 10.44.40.33 | Elasticsearch es03 | 8 GB |
| **ISMELKESNODE04** | ismelkesnode04.ocplab.net | 10.44.40.34 | Elasticsearch **es04** (hot data node) | 8 GB |
| **ISMELKKBNNODE01** | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Kibana + Elastic Agent | 8 GB |
| **ISMELKFLNODE01** | ismelkflnode01.ocplab.net | 10.44.40.42 | Fleet Server + agent (+ lab APM) | 8 GB + swap |

Disk / NFS helpers for es04: `Add-Es04AndNfsDisk.ps1`, `scripts/setup-es-nfs-repo-client.sh`.

### Elasticsearch node roles (lab)

| Node | Typical roles | Notes |
|------|---------------|--------|
| **es01** | master, remote_cluster_client, **data_content** | Content tier; not master-only |
| **es02** | master, remote_cluster_client, **data_content** | Often elected master |
| **es03** | master, remote_cluster_client, **data_content**, **data_hot** | Hot tier on 8.18 side when es04 is mixed-version |
| **es04** | **data_hot**, ingest, remote_cluster_client, transform | Fourth ES node; often held on **8.19.x** during mixed tests |

- **Cluster name:** `ism-elk-cluster`
- **Domain:** `ocplab.net`
- **Data path:** `/data/elasticsearch` (lab); certs under `/etc/elasticsearch/certs`
- **Snapshot repo:** `fs_nfs_snapshots` (NFS, typically `/mnt/es-snapshots` → `path.repo`)
- **Baseline version:** Elasticsearch / Kibana / Elastic Agent **8.18.4**
- **Optional ES target:** rolling upgrade to **8.19.18** (`upgrade_es_to_8_19_18.py`)
- **Fleet / agents:** elastic-agent **tar.gz** (not RPM)
- **Install method:** RPM (ES / Kibana); archive (Fleet + agents)

### Access URLs

| Service | URL |
|---------|-----|
| Elasticsearch (es01) | https://ismelkesnode01.ocplab.net:9200 |
| Elasticsearch (es02) | https://ismelkesnode02.ocplab.net:9200 |
| Elasticsearch (es03) | https://ismelkesnode03.ocplab.net:9200 |
| Elasticsearch (**es04**) | https://ismelkesnode04.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet Server | https://ismelkflnode01.ocplab.net:8220 |
| APM intake (lab) | http://ismelkflnode01.ocplab.net:8200 |

Elastic superuser password: `secrets/elastic-password` or `python show_elastic_password.py`. **Do not commit passwords.**

## Prerequisites

- Windows host with Hyper-V, PowerShell 5.1+
- Python 3.10+ with `paramiko`, `scp` on the orchestrator machine
- RHEL 8.10 VMs with network access between nodes
- Offline packages in `packages/` (not in git): Elasticsearch/Kibana RPMs (8.18.4, 8.19.18, …), `elastic-agent-*-linux-x86_64.tar.gz`

## Quick start

### 1. Configuration

```powershell
python init_config.py
# Or: Copy-Item config.psd1.example config.psd1  then edit manually
```

`init_config.py` prompts for domain, hostname, IP, disk size, and OS root password on first run and writes `config.psd1`.

`config.psd1` is gitignored — never commit real passwords.

### 2. Create VMs (PowerShell, elevated)

```powershell
.\New-ElasticClusterVMs.ps1
# es04 + NFS data disk (if not part of base create):
.\Add-Es04AndNfsDisk.ps1
```

### 3. Full ordered deploy

```powershell
python run_with_pass.py deploy_ordered_stack.py
```

Phases: ES cluster → Kibana → Fleet Server → agent policies → agents.

### 4. Air-gapped Fleet (no epr.elastic.co)

```powershell
python run_with_pass.py deploy_local_epr.py
python run_with_pass.py redeploy_fleet_only.py
python run_with_pass.py resume_agent_deploy.py
```

### 5. Verify

```powershell
python run_with_pass.py fleet_ps.py
python run_with_pass.py verify_kibana.py
```

## Lab ops (8.18.4 / 8.19.18, Hyper-V, APM, es04 rejoin)

Full narrative: **[docs/LAB_OPS_8_18_8_19.md](docs/LAB_OPS_8_18_8_19.md)**. Live status: **[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)**.

### Command cheat sheet

| Goal | Command |
|------|---------|
| Rolling ES upgrade → **8.19.18** (all joined ES nodes, timed) | `python upgrade_es_to_8_19_18.py` |
| Hyper-V checkpoint ES nodes (incl. **es04**) | `.\Checkpoint-EsNodes.ps1` |
| Hyper-V checkpoint full stack (ES + Kibana + Fleet) | `.\Checkpoint-AllStackVMs.ps1` |
| Restore all ES nodes from a named snap | `.\Restore-EsNodes-To-Snap.ps1 -SnapshotName <name>` |
| Restore **es01–es03** only (leave es04 running) | `.\Restore-Es01to03-To-Snap.ps1 -SnapshotName <name>` |
| Rename / remove+retake Hyper-V snaps | `Rename-HyperVSnaps.ps1`, `Remove-And-Retake-HyperVSnap.ps1` |
| Strip es01–es03 to data_content + HV snap | `python roles_data_content_and_hv_snap.py` |
| Add **data_hot** on es03, test es04 rejoin | `python es03_hot_then_es04_rejoin.py` |
| HV restore es01–es03 + hot + es04 rejoin | `python restore_es01_03_hot_es04_rejoin.py` |
| Per-node RPM downgrade to 8.18.4 | `scripts/downgrade-es-node-8184.sh` |
| Full wipe + NFS snapshot restore lab path | `python complete_downgrade_restore.py` |
| Finish restore / es04 join checks | `python finish_downgrade_restore_es04.py` |
| Reduce yellow (mixed version) | `python fix_yellow_mixed_version.py` |
| APM Server on Fleet host | `python deploy_apm_finish.py` |
| Sample Obs/APM alerts + ES/HV snaps | `python create_obs_apm_alerts_and_snaps.py` |
| NFS snapshot export / ES client | `scripts/setup-nfs-snapshot-export.sh`, `scripts/setup-es-nfs-repo-client.sh` |

### Hyper-V snapshot names (lab convention)

| Snapshot | Purpose |
|----------|---------|
| `pre-upgrade-system-8.18.4-20260724` | Clean 8.18.4 before first upgrade wave |
| `pre-upgrade-system-8.18.4-with-apm` | 8.18.4 + APM + observability/APM alert rules |
| `post-upgrade-system-8.19.18-20260724` | After rolling ES to 8.19.18 |

Useful Elasticsearch snapshots in repo `fs_nfs_snapshots`:

- `pre-upgrade-system-8.18.4-20260724`
- `pre-upgrade-system-8.18.4-with-apm-20260724`

### es04 rejoin rules (important)

| Scenario | Result |
|----------|--------|
| es01–es03 **RPM wipe** (new cluster UUID) + snapshot restore; es04 still 8.19 with old cluster metadata | **es04 does not rejoin** |
| es01–es03 **Hyper-V restore** to `pre-upgrade-system-8.18.4-with-apm` (same cluster UUID as es04) | **es04 rejoins** (mixed-version rolling path) |
| All ES nodes version-aligned | Normal green when tiers/replicas match capacity |

Without at least one **data_hot** node on the 8.18 side (lab: **es03**), hot-tier indices stay unassigned even if es04 is down.

### Mixed-version yellow health

When es01–es03 are **8.18.4** and **es04** is **8.19.x**:

1. Run `python fix_yellow_mixed_version.py` — sets `number_of_replicas: 0` and **`auto_expand_replicas: false`** on non-system indices (auto-expand alone puts replicas back to 1).
2. Residual **yellow** with ~4 unassigned **replicas** on system/restricted indices (`.security-7`, `.security-profile-8`, `.fleet-actions-results`) is **expected**. Elastic blocks changing those settings; older nodes cannot host replicas of primaries on a newer node.
3. All **primaries** assigned + high `active_shards_percent` is normal for this lab state.
4. True **green** requires version alignment:
   - Preferred: upgrade es01–es03 to 8.19.18, **or**
   - Restore **all** ES nodes (including **es04**) to the same 8.18.4 Hyper-V snap.

### Downgrade notes (8.19 → 8.18)

**Binary downgrade of Elasticsearch data directories is not supported.** After package reinstall you must wipe `path.data` and restore from an 8.18-compatible snapshot.

1. Preserve `/etc/elasticsearch/certs` and `elasticsearch.keystore` across RPM reinstall.
2. Re-apply production `elasticsearch.yml` (package defaults overwrite lab paths/roles).
3. Snapshot restore: use **explicit** `feature_states` (`security`, `kibana`, `fleet`, `async_search`, …) — `["*"]` is rejected.
4. After wipe, elastic password is new until security feature state restore; then the snapshot password applies.
5. Procedure report from the lab run: `logs/downgrade_es01_03_procedure_report.txt`.

### APM + alerts

```powershell
python deploy_apm_finish.py                 # Standalone APM Server on Fleet host (air-gapped Fleet APM package may be stub)
python create_obs_apm_alerts_and_snaps.py   # Sample observability / APM rules + snaps
```

APM intake: `http://10.44.40.42:8200` (ismelkflnode01).

## Upgrade procedures (general / 9.x path)

### Download upgrade packages (once)

```powershell
python download_upgrade_packages.py
```

Downloads Elasticsearch/Kibana RPMs and agent archives for **8.19.9** / **8.19.18** and **9.4.1** into `packages/`.

### Create pre-upgrade checkpoints (stack VMs)

```powershell
.\Snapshot-ElasticVMs.ps1
# Default name: pre-upgrade-9.4.1-YYYYMMDD-HHmm
# Or ES-focused:
.\Checkpoint-EsNodes.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
.\Checkpoint-AllStackVMs.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
```

### Full stack upgrade (ES + Kibana + agents)

Rolling path: **8.18.4 → 8.19.x → 9.4.1**

```powershell
python upgrade_elastic_stack.py
```

Upgrades ES nodes (including **es04** when joined), Kibana, and Fleet-managed agents via artifact mirror.

### ES-only upgrade to 8.19.18 (lab)

```powershell
python upgrade_es_to_8_19_18.py
```

Non-master nodes first; master last. Logs: `logs/upgrade_es_8_19_18.log`.

### ES-only upgrade (older 9.x helper)

```powershell
python upgrade_es_only.py
```

**Note:** Kibana 8.18.4 against Elasticsearch 9.x is outside Elastic's supported matrix. Lab/testing only.

### Fleet rollback + artifact upgrade

```powershell
python rollback_upgrade_fleet.py
python rollback_reinstall_fleet.py   # fallback
python fleet_bulk_upgrade_agents.py  # when Fleet already at target
```

### Restore VMs from checkpoint

```powershell
.\Restore-ElasticVMs.ps1 -SnapshotName pre-upgrade-9.4.1-20260629-1535
.\Restore-EsNodes-To-Snap.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
.\Restore-Es01to03-To-Snap.ps1 -SnapshotName pre-upgrade-system-8.18.4-with-apm
# Or:
python restore_elastic_vms.py
```

## NFS snapshot repository

```text
scripts/setup-nfs-snapshot-export.sh   # export side
scripts/install-nfs-from-iso.sh        # if NFS client packages from ISO
scripts/setup-es-nfs-repo-client.sh    # mount + path.repo on ES nodes
python setup_nfs_snapshot_roles.py
```

Repo name in Elasticsearch: **`fs_nfs_snapshots`**.

## x.509 / Elasticsearch CA

Fleet and agents trust the ES auto-configured CA via `scripts/elastic-agent-ca.sh`:

- Stages `http_ca.crt` under `/etc/elastic-agent/certs/`
- Fleet Server: `--certificate-authorities`, `--fleet-server-es-ca`, `--fleet-server-es-ca-trusted-fingerprint`
- Agents: `--certificate-authorities` + `--insecure` (Fleet 8220 self-signed only)

## Key scripts

### Deploy / Fleet / agents

| Script | Purpose |
|--------|---------|
| `deploy_ordered_stack.py` | Full phased deploy orchestrator |
| `deploy_local_epr.py` | Local EPR mock + air-gap Fleet config |
| `redeploy_fleet_only.py` | Fleet Server only (with CA); skips if healthy |
| `resume_agent_deploy.py` | Agent policies + deploy; skips if agents up |
| `scripts/install-fleet-server.sh` | Archive Fleet Server + custom CA |
| `scripts/install-elastic-agent.sh` | Archive agent enroll + custom CA |
| `show_elastic_password.py` | Find current elastic password without reset |
| `create_cluster_ops_dashboard.py` | Cluster operations Kibana dashboard |

### Upgrade / downgrade / yellow

| Script | Purpose |
|--------|---------|
| `upgrade_es_to_8_19_18.py` | Timed rolling ES upgrade to 8.19.18 (es01–**es04**) |
| `upgrade_elastic_stack.py` | Full stack rolling upgrade toward 9.4.1 |
| `upgrade_es_only.py` | Restore snapshots + ES-only upgrade helper |
| `download_upgrade_packages.py` | Fetch offline RPMs/archives |
| `scripts/downgrade-es-node-8184.sh` | Per-node RPM reinstall to 8.18.4 |
| `complete_downgrade_restore.py` | Wipe + NFS snapshot restore lab path |
| `finish_downgrade_restore_es04.py` | Post-restore / **es04** join checks |
| `fix_yellow_mixed_version.py` | Clear non-system unassigned replicas |
| `rollback_upgrade_fleet.py` | Fleet artifact rollback/upgrade |
| `rollback_reinstall_fleet.py` | Fleet rollback via direct reinstall |
| `fleet_bulk_upgrade_agents.py` | Agent bulk_upgrade when Fleet at target |
| `scripts/upgrade-elasticsearch-node.sh` | Single-node ES rolling upgrade |
| `scripts/upgrade-kibana.sh` | Kibana RPM upgrade |
| `scripts/upgrade-elastic-agent.sh` | Local agent archive upgrade |

### Hyper-V / roles / APM / es04

| Script | Purpose |
|--------|---------|
| `Add-Es04AndNfsDisk.ps1` | Create/attach **es04** disk / NFS-related setup |
| `Checkpoint-EsNodes.ps1` | HV checkpoint es01–**es04** |
| `Checkpoint-AllStackVMs.ps1` | HV checkpoint ES + Kibana + Fleet |
| `Restore-EsNodes-To-Snap.ps1` | Restore all ES nodes from named snap |
| `Restore-Es01to03-To-Snap.ps1` | Restore es01–es03 only (es04 stays up) |
| `Rename-HyperVSnaps.ps1` | Rename Hyper-V checkpoints |
| `Remove-And-Retake-HyperVSnap.ps1` | Delete + retake named snap |
| `Remove-AllHyperVSnaps-And-Snapshot.ps1` | Clear all snaps then take one |
| `Snapshot-ElasticVMs.ps1` / `Restore-ElasticVMs.ps1` | Original full-stack snap/restore |
| `run_hv_snap_elevated.py` / `run_hv_retake_post81918.py` | UAC elevation wrappers |
| `roles_data_content_and_hv_snap.py` | data_content roles + HV snap |
| `es03_hot_then_es04_rejoin.py` | es03 data_hot + es04 rejoin test |
| `restore_es01_03_hot_es04_rejoin.py` | HV restore es01–03 + hot + es04 |
| `rejoin_es04_8184_and_snapshot_repo.py` | es04 rejoin + snapshot repo wiring |
| `join_es04_via_certs.py` | Join es04 using cluster certs |
| `deploy_apm_finish.py` | Standalone APM Server on Fleet host |
| `create_obs_apm_alerts_and_snaps.py` | Sample Obs/APM rules + snaps |
| `add_es04_integrations.py` | Fleet integrations for es04 agent |

## License

Internal lab use. Elastic Stack is subject to [Elastic License](https://www.elastic.co/licensing/elastic-license).
