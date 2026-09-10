# Lab operations: 8.19.18 → 9.5.3 (prepare, then upgrade)

Elastic requires **8.19.x** before any upgrade to **9.1 or later**. The current 9.x target for this lab is **9.5.3**. Stay on **8.19.18** until Upgrade Assistant is clean and snapshots exist.

Official path (self-managed): Elasticsearch → Kibana (same version) → Fleet Server / APM → ingest tools (Elastic Agent). 8.19 agents remain compatible with 9.x Elasticsearch, but this lab upgrades agents in lockstep.

## Prerequisites (must be true)

1. All four ES nodes on **8.19.18** (not mixed 8.18/8.19).
2. Kibana RPM **8.19.18**.
3. Fleet Server + enrolled agents on **8.19.x** (lab: 8.19.18).
4. Offline packages in `packages/` (gitignored):

   - `elasticsearch-9.5.3-x86_64.rpm`
   - `kibana-9.5.3-x86_64.rpm`
   - `elastic-agent-9.5.3-linux-x86_64.tar.gz` (+ `.sha512` / `.asc`)

   ```powershell
   python download_upgrade_packages.py
   ```

5. NFS repo `fs_nfs_snapshots` registered. Hyper-V checkpoints available.

## Preflight (does not upgrade)

```powershell
python prepare_upgrade_9_5_3.py
```

This checks packages, node versions, `_migration/deprecations`, and the snapshot repo. Resolve **critical** deprecations in Kibana **Upgrade Assistant** (`Stack Management → Upgrade Assistant`) on 8.19.18 before going to 9.5.3.

## Snapshots immediately before 9.5.3

```powershell
python create_named_snapshot.py --name pre-upgrade-to-9.5.3 --because "8.19.18 before 9.5.3"
.\Checkpoint-AllStackVMs.ps1 -SnapshotName pre-upgrade-to-9.5.3
```

Do **not** use `recreate_preupgrade_snap.py` here — it deletes a named older snap.

## Perform the 9.5.3 upgrade

```powershell
python upgrade_elastic_stack.py --to 9.5.3
```

Order inside the orchestrator:

1. Rolling ES (non-master first, current master last; skip nodes already on target).
2. Kibana RPM 9.5.3.
3. Fleet-managed agents via local artifact mirror + `bulk_upgrade`.

Partial reruns:

```powershell
python upgrade_elastic_stack.py --to 9.5.3 --skip-es
python upgrade_elastic_stack.py --to 9.5.3 --skip-es --skip-kibana
python upgrade_agents_only.py --version 9.5.3
```

## Rollback

- Hyper-V: `.\Restore-EsNodes-To-Snap.ps1 -SnapshotName pre-upgrade-to-9.5.3` (or `Restore-ElasticVMs.ps1` for all six VMs).
- ES data directories **cannot** be binary-downgraded. After a failed 9.x RPM install, wipe `path.data` and restore the 8.19 NFS snapshot, preserving certs/keystore and lab `elasticsearch.yml`.
- Snapshot restore: explicit `feature_states` (never `["*"]`).

## After 9.5.3

1. `_cluster/health`, `_cat/nodes?v` — four nodes on 9.5.3, no unassigned primaries.
2. Kibana `/api/status` available.
3. Fleet `:8220` HEALTHY; agents online on 9.5.3 (`python print_stack_versions.py`).
4. Re-check ILM / stack-monitoring dashboards; Lens metric `palette` rules from 8.18 still apply if panels go empty.
5. Update `DEPLOYMENT_STATUS.md` and push.

## Do not

- Jump 8.18.4 → 9.5.3 in one shot (`upgrade_elastic_stack.py` refuses 9.x unless every ES node is already 8.19.x).
- Unenroll the **online** Fleet Server host to “clean ghosts”.
- Commit `packages/`, `config.psd1`, or `secrets/*`.
