#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Remove a named Hyper-V checkpoint on ELK VMs (if present), then create it again.
#>
[CmdletBinding()]
param(
    [string]$SnapshotName = 'post-upgrade-system-8.19.18-20260724',
    [string]$LogPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\logs\hv-retake-post-81918.log'
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

$vms = @(
    'ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03', 'ISMELKESNODE04',
    'ISMELKKBNNODE01', 'ISMELKFLNODE01'
)

W "START remove+retake SnapshotName=$SnapshotName"

foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        W "WARN missing VM $vmName"
        continue
    }
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    W "=== $vmName State=$($vm.State) existing=$($snaps.Count) names=$($snaps.Name -join ';') ==="

    $target = $snaps | Where-Object { $_.Name -eq $SnapshotName } | Select-Object -First 1
    if ($target) {
        try {
            # Remove this checkpoint only (children may become independent of it)
            W "  Remove-VMSnapshot '$SnapshotName'"
            Remove-VMSnapshot -VMSnapshot $target -IncludeAllChildSnapshots:$false -ErrorAction Stop
            W "  removed OK"
        } catch {
            # If snapshot has children, Hyper-V may require different handling
            try {
                W "  retry Remove-VMSnapshot without child flag: $_"
                Remove-VMSnapshot -VMName $vmName -Name $SnapshotName -ErrorAction Stop
                W "  removed OK (by name)"
            } catch {
                W "  ERROR remove: $_"
            }
        }
    } else {
        W "  no snapshot named $SnapshotName (skip remove)"
    }
}

Start-Sleep -Seconds 3

W "=== Create checkpoints: $SnapshotName ==="
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) { continue }
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    if ($snaps | Where-Object { $_.Name -eq $SnapshotName }) {
        W "  SKIP $vmName already has $SnapshotName after remove?"
        continue
    }
    try {
        W "  Checkpoint-VM $vmName -> $SnapshotName"
        Checkpoint-VM -Name $vmName -SnapshotName $SnapshotName -ErrorAction Stop
        W "  OK $vmName"
    } catch {
        W "  ERROR create $vmName : $_"
    }
}

W "=== VERIFY ==="
$ok = $true
foreach ($vmName in $vms) {
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    $has = $snaps | Where-Object { $_.Name -eq $SnapshotName }
    W "$vmName count=$($snaps.Count) names=$($snaps.Name -join ', ') hasTarget=$([bool]$has)"
    if (-not $has) { $ok = $false }
}
if ($ok) { W "DONE" } else { W "DONE_WITH_ERRORS"; exit 1 }
exit 0
