#Requires -Modules Hyper-V
<#
.SYNOPSIS
    End-to-end flash redeploy: RHEL 8.10 OS + VHD + Elasticsearch + Kibana + Fleet + Agents.

.EXAMPLE
    .\Flash-Deploy-All.ps1 -Recreate
    .\Flash-Deploy-All.ps1 -Recreate -SkipWait   # OS install only
#>
[CmdletBinding()]
param(
    [switch]$Recreate,
    [switch]$SkipWait,
    [string]$ConfigPath
)

$ScriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ConfigPath) { $ConfigPath = Join-Path $ScriptRoot 'config.psd1' }

$config = Import-PowerShellDataFile -Path $ConfigPath
$env:SSH_PASS = $config.RootPassword

Write-Host "=== Phase 1: Flash OS install (RHEL $($config.RHELVersion)) ===" -ForegroundColor Cyan
& (Join-Path $ScriptRoot 'Flash-Install-ELKStack.ps1') -ConfigPath $ConfigPath -Recreate:$Recreate

if ($SkipWait) {
    Write-Host "Skipping post-install wait. Run deploy_full_stack.py when OS install completes."
    exit 0
}

Write-Host "`n=== Phase 2: Waiting for OS install (SSH) ===" -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    $allUp = $true
    foreach ($node in $config.Nodes) {
        $tcp = Test-NetConnection -ComputerName $node.IPAddress -Port 22 -WarningAction SilentlyContinue
        if (-not $tcp.TcpTestSucceeded) { $allUp = $false; break }
    }
    if ($allUp) {
        $ready = $true
        Write-Host "All nodes reachable on SSH." -ForegroundColor Green
        break
    }
    Write-Host "  Waiting... ($($i * 30)s)"
    Start-Sleep -Seconds 30
}

if (-not $ready) {
    Write-Warning "Not all nodes on SSH yet. Run manually: python $ScriptRoot\deploy_full_stack.py"
    exit 1
}

Write-Host "`n=== Phase 3: Elastic Stack + Fleet + Agents ===" -ForegroundColor Cyan
python (Join-Path $ScriptRoot 'deploy_full_stack.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`nFlash deploy complete." -ForegroundColor Green