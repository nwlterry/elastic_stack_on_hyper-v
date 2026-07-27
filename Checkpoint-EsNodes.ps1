#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Create an additional Hyper-V checkpoint on Elasticsearch VMs only (keep existing snaps).
#>
[CmdletBinding()]
param(
    [string]$SnapshotName = 'post-upgrade-system-8.19.18-20260724',
    [string]$LogPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\logs\hv-es-post-81918-snap.log'
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

$vms = @('ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03', 'ISMELKESNODE04')

W "START SnapshotName=$SnapshotName (ES nodes only; keep existing checkpoints)"
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        W "WARN missing VM $vmName"
        continue
    }
    $existing = Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue
    W "=== $vmName State=$($vm.State) existing=$($existing.Count) names=$($existing.Name -join ';') ==="
    $dup = $existing | Where-Object { $_.Name -eq $SnapshotName } | Select-Object -First 1
    if ($dup) {
        W "  SKIP already has $SnapshotName"
        continue
    }
    try {
        W "  Checkpoint-VM -> $SnapshotName"
        Checkpoint-VM -Name $vmName -SnapshotName $SnapshotName -ErrorAction Stop
        W "  OK $vmName"
    } catch {
        W "  ERROR $vmName : $_"
    }
}

W "=== VERIFY ==="
$ok = $true
foreach ($vmName in $vms) {
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    $names = $snaps.Name -join ', '
    $hasNew = $snaps | Where-Object { $_.Name -eq $SnapshotName }
    W "$vmName count=$($snaps.Count) names=$names"
    if (-not $hasNew) { $ok = $false }
}
if ($ok) { W "DONE" } else { W "DONE_WITH_ERRORS"; exit 1 }
exit 0
