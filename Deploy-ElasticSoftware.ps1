#Requires -Modules Hyper-V
<#
.SYNOPSIS
    Copy install scripts to ELK VMs and run Elasticsearch 8.18.4 setup.

.PARAMETER SshUser
    SSH user with sudo on RHEL nodes (default: root).

.PARAMETER EsNodesOnly
    Run only on Elasticsearch nodes.

.EXAMPLE
    .\Deploy-ElasticSoftware.ps1 -SshUser root
#>
[CmdletBinding()]
param(
    [string]$SshUser = 'root',
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.psd1'),
    [switch]$EsNodesOnly
)

$config = Import-PowerShellDataFile -Path $ConfigPath
$scriptDir = Join-Path $PSScriptRoot 'scripts'
$remoteDir = '/opt/elastic-setup'

$esNodes = @(
    @{ Name = $config.Elasticsearch.Names[0]; IP = $config.Elasticsearch.IPAddresses[0] }
    @{ Name = $config.Elasticsearch.Names[1]; IP = $config.Elasticsearch.IPAddresses[1] }
    @{ Name = $config.Elasticsearch.Names[2]; IP = $config.Elasticsearch.IPAddresses[2] }
)

$kibana = @{
    Name = $config.Kibana.Name
    IP   = $config.Kibana.IPAddress
}

function Invoke-Remote {
    param([string]$IP, [string]$Command)
    ssh -o StrictHostKeyChecking=no "${SshUser}@${IP}" $Command
}

function Copy-Scripts {
    param([string]$IP)
    ssh -o StrictHostKeyChecking=no "${SshUser}@${IP}" "mkdir -p $remoteDir"
    scp -o StrictHostKeyChecking=no -r "$scriptDir\*" "${SshUser}@${IP}:${remoteDir}/"
}

Write-Host "=== Deploying to Elasticsearch nodes ===" -ForegroundColor Cyan
foreach ($node in $esNodes) {
    Write-Host "-> $($node.Name) ($($node.IP))" -ForegroundColor Yellow
    Copy-Scripts -IP $node.IP
    Invoke-Remote -IP $node.IP -Command "chmod +x ${remoteDir}/*.sh"
    Invoke-Remote -IP $node.IP -Command "sudo bash ${remoteDir}/prepare-data-disk.sh"
    Invoke-Remote -IP $node.IP -Command "sudo bash ${remoteDir}/install-elasticsearch.sh --version $($config.ElasticVersion) --node $($node.Name) --cluster $($config.ClusterName)"
}

if (-not $EsNodesOnly) {
    Write-Host "`n=== Kibana node (install only — enroll after bootstrap) ===" -ForegroundColor Cyan
    Copy-Scripts -IP $kibana.IP
    Invoke-Remote -IP $kibana.IP -Command "chmod +x ${remoteDir}/*.sh"
    Invoke-Remote -IP $kibana.IP -Command "sudo bash ${remoteDir}/install-kibana.sh --version $($config.ElasticVersion) --es-host $($config.Elasticsearch.IPAddresses[0])"
}

Write-Host @"

=== Manual cluster bootstrap (interactive) ===
1. On $($config.Elasticsearch.Names[0]):
     ssh ${SshUser}@$($config.Elasticsearch.IPAddresses[0])
     sudo bash ${remoteDir}/bootstrap-cluster.sh

2. On $($config.Elasticsearch.Names[1]) and $($config.Elasticsearch.Names[2]):
     NODE_ENROLLMENT_TOKEN='<token>' sudo bash ${remoteDir}/bootstrap-cluster.sh

3. Enroll Kibana with token from step 1:
     sudo bash ${remoteDir}/install-kibana.sh --version $($config.ElasticVersion) --es-host $($config.Elasticsearch.IPAddresses[0]) --enrollment-token '<token>'

"@