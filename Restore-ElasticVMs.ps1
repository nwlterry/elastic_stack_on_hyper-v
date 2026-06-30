#Requires -Modules Hyper-V
<#
.SYNOPSIS
    Restore all ELK stack VMs from a Hyper-V checkpoint (rollback baseline).

.DESCRIPTION
    Stops each VM, restores the named snapshot, then starts in order:
    ES01 -> ES02 -> ES03 -> Kibana -> Fleet.

.EXAMPLE
    .\Restore-ElasticVMs.ps1 -SnapshotName pre-upgrade-9.4.1-20260629-1535
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\config.psd1',
    [string]$SnapshotName = 'pre-upgrade-9.4.1-20260629-1535'
)

$config = Import-PowerShellDataFile -Path $ConfigPath
$vmNames = @($config.Nodes | ForEach-Object { $_.VMName })
$startOrder = @(
    'ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03',
    'ISMELKKBNNODE01', 'ISMELKFLNODE01'
)

foreach ($vmName in $vmNames) {
    $snap = Get-VMSnapshot -VMName $vmName -Name $SnapshotName -ErrorAction SilentlyContinue
    if (-not $snap) {
        throw "Snapshot '$SnapshotName' not found on $vmName"
    }
}

Write-Host "Stopping $($vmNames.Count) VMs..." -ForegroundColor Cyan
foreach ($vmName in $vmNames) {
    Stop-VM -Name $vmName -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 10

foreach ($vmName in $vmNames) {
    Write-Host "Restore $vmName <- $SnapshotName" -ForegroundColor Cyan
    Restore-VMSnapshot -VMName $vmName -Name $SnapshotName -Confirm:$false
}

foreach ($vmName in $startOrder) {
    if ($vmName -notin $vmNames) { continue }
    Write-Host "Start $vmName" -ForegroundColor Green
    Start-VM -Name $vmName
    Start-Sleep -Seconds 5
}

Write-Host "`nRestored $($vmNames.Count) VMs from $SnapshotName" -ForegroundColor Green
Write-Host "Run: python upgrade_es_only.py  (ES-only upgrade, Kibana/Fleet stay at 8.18.4)"