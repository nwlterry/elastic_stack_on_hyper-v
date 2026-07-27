#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Restore only ISMELKESNODE01-03 to a named Hyper-V checkpoint. es04 left running.
#>
[CmdletBinding()]
param(
    [string]$SnapshotName = 'pre-upgrade-system-8.18.4-with-apm',
    [string]$LogPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\logs\hv-restore-es01-03-with-apm.log'
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

$vms = @('ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03')
$startOrder = @('ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03')

W "START restore SnapshotName=$SnapshotName es01-es03 only (es04 untouched)"

foreach ($vmName in $vms) {
    $snap = Get-VMSnapshot -VMName $vmName -Name $SnapshotName -ErrorAction SilentlyContinue
    if (-not $snap) { throw "Snapshot '$SnapshotName' not found on $vmName" }
    W "OK snap on $vmName"
}

foreach ($vmName in $vms) {
    W "Stop-VM $vmName"
    Stop-VM -Name $vmName -Force -TurnOff -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 12

foreach ($vmName in $vms) {
    W "Restore-VMSnapshot $vmName <- $SnapshotName"
    Restore-VMSnapshot -VMName $vmName -Name $SnapshotName -Confirm:$false
    W "  restored $vmName"
}

foreach ($vmName in $startOrder) {
    W "Start-VM $vmName"
    Start-VM -Name $vmName
    Start-Sleep -Seconds 8
}

W "=== VERIFY ==="
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName
    W "$vmName State=$($vm.State)"
}
W "DONE"
exit 0
