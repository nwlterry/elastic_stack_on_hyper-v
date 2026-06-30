# Elastic Stack on Hyper-V (RHEL 8.10)

Automated deployment and upgrade of **Elasticsearch**, **Kibana**, **Fleet Server**, and **Elastic Agents** on five RHEL 8.10 Hyper-V VMs with air-gapped Fleet and custom CA enrollment.

See **[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)** for the latest verified stack state, policy IDs, and safe commands.

## Architecture

| VM | FQDN | IP | Role | RAM |
|----|------|-----|------|-----|
| ISMELKESNODE01–03 | ismelkesnode01–03.ocplab.net | 10.44.40.31–33 | Elasticsearch (3-node cluster) | 8 GB each |
| ISMELKKBNNODE01 | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Kibana + agent | 8 GB |
| ISMELKFLNODE01 | ismelkflnode01.ocplab.net | 10.44.40.42 | Fleet Server + agent | 8 GB + swap |

- **Cluster:** `ism-elk-cluster`
- **Domain:** `ocplab.net`
- **Baseline version:** Elasticsearch / Kibana / Elastic Agent **8.18.4**
- **Fleet / agents:** elastic-agent **tar.gz** (not RPM)

## Prerequisites

- Windows host with Hyper-V, PowerShell 5.1+
- Python 3.10+ with `paramiko`, `scp` on the orchestrator machine
- RHEL 8.10 VMs with network access between nodes
- Offline packages in `packages/` (not in git): Elasticsearch/Kibana RPMs, `elastic-agent-*-linux-x86_64.tar.gz`

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

## Upgrade procedures

### Download upgrade packages (once)

```powershell
python download_upgrade_packages.py
```

Downloads Elasticsearch/Kibana RPMs and agent archives for **8.19.9** and **9.4.1** into `packages/`.

### Create pre-upgrade checkpoints (all 5 VMs)

```powershell
.\Snapshot-ElasticVMs.ps1
# Default name: pre-upgrade-9.4.1-YYYYMMDD-HHmm
```

### Full stack upgrade (ES + Kibana + agents)

Rolling path: **8.18.4 → 8.19.9 → 9.4.1**

```powershell
python upgrade_elastic_stack.py
```

Upgrades all ES nodes, Kibana, and Fleet-managed agents via artifact mirror.

### ES-only upgrade (Kibana + Fleet stay at 8.18.4)

Restore all VMs to the pre-upgrade snapshot, then upgrade Elasticsearch nodes only:

```powershell
python upgrade_es_only.py
```

Or restore snapshots manually, then upgrade ES:

```powershell
.\Restore-ElasticVMs.ps1 -SnapshotName pre-upgrade-9.4.1-20260629-1535
python -c "from upgrade_elastic_stack import *; from deploy_ordered_stack import *; ..."
```

**Note:** Kibana 8.18.4 against Elasticsearch 9.4.1 is outside Elastic's supported matrix. Use for lab/testing only.

### Fleet rollback + artifact upgrade

When Fleet Server needs a stepped upgrade (cannot `bulk_upgrade` itself while serving `:8220`):

```powershell
python rollback_upgrade_fleet.py
```

Steps: unenroll Fleet agents → restore Fleet VM snapshot → re-enroll Fleet @ 8.18.4 → stepped enroll to 8.19.9 → 9.4.1 → `bulk_upgrade` other agents.

Fallback (direct Fleet reinstall @ target):

```powershell
python rollback_reinstall_fleet.py
```

When Fleet is already at target and only other agents need upgrading:

```powershell
python fleet_bulk_upgrade_agents.py
```

### Restore all VMs from checkpoint

```powershell
.\Restore-ElasticVMs.ps1 -SnapshotName pre-upgrade-9.4.1-20260629-1535
# Or:
python restore_elastic_vms.py
```

## x.509 / Elasticsearch CA

Fleet and agents trust the ES auto-configured CA via `scripts/elastic-agent-ca.sh`:

- Stages `http_ca.crt` under `/etc/elastic-agent/certs/`
- Fleet Server: `--certificate-authorities`, `--fleet-server-es-ca`, `--fleet-server-es-ca-trusted-fingerprint`
- Agents: `--certificate-authorities` + `--insecure` (Fleet 8220 self-signed only)

## Key scripts

| Script | Purpose |
|--------|---------|
| `deploy_ordered_stack.py` | Full phased deploy orchestrator |
| `deploy_local_epr.py` | Local EPR mock + air-gap Fleet config |
| `redeploy_fleet_only.py` | Fleet Server only (with CA); skips if healthy |
| `resume_agent_deploy.py` | Agent policies + deploy; skips if 5 agents up |
| `Snapshot-ElasticVMs.ps1` | Create Hyper-V checkpoints on all 5 VMs |
| `Restore-ElasticVMs.ps1` | Restore all 5 VMs from a named checkpoint |
| `upgrade_elastic_stack.py` | Full stack rolling upgrade to 9.4.1 |
| `upgrade_es_only.py` | Restore snapshots + ES-only upgrade |
| `rollback_upgrade_fleet.py` | Fleet artifact rollback/upgrade (primary) |
| `rollback_reinstall_fleet.py` | Fleet rollback via direct reinstall (fallback) |
| `fleet_bulk_upgrade_agents.py` | Agent bulk_upgrade when Fleet already at target |
| `download_upgrade_packages.py` | Fetch offline RPMs/archives for upgrade |
| `scripts/install-fleet-server.sh` | Archive Fleet Server + custom CA |
| `scripts/install-elastic-agent.sh` | Archive agent enroll + custom CA |
| `scripts/upgrade-elasticsearch-node.sh` | Single-node ES rolling upgrade |
| `scripts/upgrade-kibana.sh` | Kibana RPM upgrade |
| `scripts/upgrade-elastic-agent.sh` | Local agent archive upgrade |
| `show_elastic_password.py` | Find current elastic password without reset |

## Access

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet | https://ismelkflnode01.ocplab.net:8220 |

## License

Internal lab use. Elastic Stack is subject to [Elastic License](https://www.elastic.co/licensing/elastic-license).