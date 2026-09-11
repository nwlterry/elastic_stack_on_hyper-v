# Lab operations: 8.19.18 → 9.5.3

Elastic requires **8.19.x** before any upgrade to **9.1 or later**. Official self-managed order: Elasticsearch → Kibana (same version) → Fleet Server → Elastic Agent.

This lab completed that path on **2026-09-11**. 8.19 agents remain compatible with 9.x Elasticsearch, but Fleet `bulk_upgrade` **cannot** jump agents to 9.x until Fleet Server itself is on 9.x.

## Prerequisites

1. All four ES nodes on **8.19.18** (not mixed 8.18/8.19).
2. Kibana RPM **8.19.18**.
3. Fleet Server + enrolled agents on **8.19.18**.
4. Offline packages in `packages/` (gitignored):

   - `elasticsearch-9.5.3-x86_64.rpm`
   - `kibana-9.5.3-x86_64.rpm`
   - `elastic-agent-9.5.3-linux-x86_64.tar.gz` (+ `.sha512` / `.asc`)

   ```powershell
   python download_upgrade_packages.py
   ```

5. NFS repo `fs_nfs_snapshots` registered (`python remount_es_nfs.py` if es02/es03 lost the mount).

## Preflight (does not upgrade)

```powershell
python prepare_upgrade_9_5_3.py
```

Resolve **critical** `_migration/deprecations` in Kibana Upgrade Assistant on 8.19.18. The 2026-09-11 run had **no** cluster/node/index deprecations.

## Snapshots immediately before 9.5.3

Keep **one** NFS snapshot so the repo does not fill the Kibana NFS export:

```powershell
python remount_es_nfs.py
python create_named_snapshot.py --reregister --name pre-upgrade-to-9.5.3 --because "8.19.18 before 9.5.3"
python prune_nfs_snapshots_keep.py --keep pre-upgrade-to-9.5.3
```

`prune_nfs_snapshots_keep.py` refuses to delete anything unless `pre-upgrade-to-9.5.3` exists and is `SUCCESS`.

Hyper-V (elevated; optional extra rollback, large on `D:`):

```powershell
.\Remove-AllHyperVSnaps-And-Snapshot.ps1 -SnapshotName pre-upgrade-to-9.5.3
```

The 2026-09-11 run kept NFS `pre-upgrade-to-9.5.3` only. Hyper-V prune needs UAC (`Desktop\Keep-Only-HV-Snap-pre-upgrade-to-9.5.3.cmd` if this session is not elevated).

Do **not** use `recreate_preupgrade_snap.py` here — it deletes a named older snap by hardcoded name.

## Perform the 9.5.3 upgrade

```powershell
python upgrade_elastic_stack.py --to 9.5.3
```

Order inside the orchestrator:

1. Rolling ES (non-master first, current master last; skip nodes already on target).
2. Kibana RPM 9.5.3.
3. Fleet-managed agents via local artifact mirror `:8081` + `bulk_upgrade`.

Partial reruns:

```powershell
python upgrade_elastic_stack.py --to 9.5.3 --skip-es
python upgrade_elastic_stack.py --to 9.5.3 --skip-es --skip-kibana
python reenroll_fleet_and_agents.py --version 9.5.3
```

## 2026-09-11 run (what actually happened)

### 1. NFS prune

Deleted:

- `pre-upgrade-system-8.18.4-20260724`
- `pre-upgrade-system-8.18.4-with-apm-20260724`
- `pre-upgrade-to-8.19.18-20260910`

Left: **`fs_nfs_snapshots/pre-upgrade-to-9.5.3` SUCCESS** (243/243 shards).

### 2. ES rolling 8.19.18 → 9.5.3

Order: es04, es03, es02, then master es01 last.

**es04 failed to start 9.5.3** with:

```
unknown setting [tracing.apm.enabled]
unknown setting [tracing.apm.agent.server_url]
unknown setting [tracing.apm.agent.environment]
```

ES 9 removed `tracing.apm.*` (ES-10293). Comment those three lines out of `/etc/elasticsearch/elasticsearch.yml` on **every** ES node **before** starting 9.x. `scripts/upgrade-elasticsearch-node.sh` now strips them automatically when `--version` is 9.x.

After the yml fix, es04 joined as 9.5.3. Mixed 8.19/9.5 is **yellow** with unassigned replicas and **zero unassigned primaries** — that is expected. Re-enable `cluster.routing.allocation.enable` if a node dies before the upgrade script's re-enable step (`primaries` was left set after the first es04 failure).

Resume is allowed with some nodes already on 9.5.3 (`require_819_before_9` accepts 8.19.x **or** the 9.x target). Wait-for-stable treats **green** as OK even with relocating shards.

Local RPM install must stay air-gapped: `dnf install -y --disablerepo='*'`.

### 3. Kibana RPM 9.5.3

`/api/status` `available`. First status probe can return empty JSON while Kibana is still coming up; wait for three consecutive `available` checks.

Kibana 9.x Fleet enrollment-token API is **`/api/fleet/enrollment_api_keys`** (underscores). The hyphenated 8.x path 404s.

### 4. Fleet / agents

`bulk_upgrade` to 9.5.3 while Fleet Server was still 8.19.18 returned:

```
Cannot force upgrade to version 9.5.3 because it does not satisfy the major and minor of the latest fleet server version 8.19.18.
```

Required sequence:

1. Re-enroll **Fleet Server only** at 9.5.3 (`python reenroll_fleet_and_agents.py --version 9.5.3`, or `reenroll_fleet_server_inplace(..., skip_vm_memory=True)` if the Fleet VM is already running).
2. Then `python upgrade_elastic_stack.py --to 9.5.3 --skip-es --skip-kibana` so remaining agents `bulk_upgrade` via `http://10.44.40.42:8081/downloads/`.
3. Unenroll the leftover **offline** 8.19.18 Fleet hostname record. Do not unenroll the **online** 9.5.3 Fleet host.

Fleet VM was started at **4 GB** because the Hyper-V host did not have 8 GB free RAM. `:8220` is healthy at 4 GB in this lab.

`pkill -f elastic-agent` in cleanup scripts can kill the SSH session that contains that string. Use path-specific pkill, and run `rpm -e` in a **separate** command.

### 5. Result (verified 2026-09-11)

| Component | Version | Notes |
|-----------|---------|--------|
| es01–es04 | **9.5.3** | cluster **green**, 4 nodes, 0 unassigned primaries |
| Kibana | **9.5.3** | `/api/status` available |
| Fleet Server | **9.5.3** | `:8220` HEALTHY |
| Agents (fleet + es01–es04 + kibana) | **9.5.3** | online |

NFS snapshots: **only** `pre-upgrade-to-9.5.3`.

## Rollback

- NFS: restore `fs_nfs_snapshots/pre-upgrade-to-9.5.3` with **explicit** `feature_states` (never `["*"]`), preserving certs/keystore and lab `elasticsearch.yml`.
- ES data directories **cannot** be binary-downgraded. After a failed 9.x RPM, wipe `path.data` and restore that snapshot.
- Hyper-V: only if a `pre-upgrade-to-9.5.3` checkpoint exists on all six VMs.

## Do not

- Jump 8.18.4 → 9.5.3 in one shot.
- Leave `tracing.apm.*` in `elasticsearch.yml` when starting 9.x.
- `bulk_upgrade` agents to 9.x before Fleet Server is on 9.x.
- Unenroll the **online** Fleet Server host to “clean ghosts”.
- Commit `packages/`, `config.psd1`, or `secrets/*`.
