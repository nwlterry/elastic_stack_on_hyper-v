# Elastic Stack on Hyper-V (RHEL 8.10)

Automated deployment of **Elasticsearch 8.18.4**, **Kibana**, **Fleet Server**, and **Elastic Agents** on five RHEL 8.10 Hyper-V VMs with air-gapped Fleet and custom CA enrollment.

See **[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)** for the latest verified stack state, policy IDs, and safe commands.

## Architecture

| VM | FQDN | IP | Role | RAM |
|----|------|-----|------|-----|
| ISMELKESNODE01–03 | ismelkesnode01–03.ocplab.net | 10.44.40.31–33 | Elasticsearch (3-node cluster) | 8 GB each |
| ISMELKKBNNODE01 | ismelkkbnnode01.ocplab.net | 10.44.40.41 | Kibana + agent | 8 GB |
| ISMELKFLNODE01 | ismelkflnode01.ocplab.net | 10.44.40.42 | Fleet Server + agent | 8 GB + swap |

- **Cluster:** `ism-elk-cluster`
- **Domain:** `ocplab.net`
- **Fleet / agents:** elastic-agent **8.18.4 tar.gz** (not RPM)

## Prerequisites

- Windows host with Hyper-V, PowerShell 5.1+
- Python 3.10+ with `paramiko`, `scp` on the orchestrator machine
- RHEL 8.10 VMs with network access between nodes
- Offline packages in `packages/` (not in git): Elasticsearch/Kibana RPMs, `elastic-agent-*-linux-x86_64.tar.gz`

## Quick start

### 1. Configuration

```powershell
Copy-Item config.psd1.example config.psd1
# Edit config.psd1: RootPassword, paths, IPs
```

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
| `scripts/install-fleet-server.sh` | Archive Fleet Server + custom CA |
| `scripts/install-elastic-agent.sh` | Archive agent enroll + custom CA |
| `scripts/configure-fleet-airgap.sh` | Kibana air-gap settings |
| `show_elastic_password.py` | Find current elastic password without reset |

## Access

| Service | URL |
|---------|-----|
| Elasticsearch | https://ismelkesnode01.ocplab.net:9200 |
| Kibana | http://ismelkkbnnode01.ocplab.net:5601 |
| Fleet | https://ismelkflnode01.ocplab.net:8220 |

## License

Internal lab use. Elastic Stack is subject to [Elastic License](https://www.elastic.co/licensing/elastic-license).