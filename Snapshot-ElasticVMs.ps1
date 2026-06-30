#Requires -Modules Hyper-V
<#
.SYNOPSIS
    Create Hyper-V checkpoints for all ELK stack VMs before upgrade.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\config.psd1',
    [string]$SnapshotName = "pre-upgrade-9.4.1-$(Get-Date -Format 'yyyyMMdd-HHmm')"
)

$config = Import-PowerShellDataFile -Path $ConfigPath
$created = @()
$skipped = @()

foreach ($node in $config.Nodes) {
    $vmName = $node.VMName
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        Write-Warning "VM not found: $vmName"
        continue
    }
    $existing = Get-VMSnapshot -VMName $vmName -Name $SnapshotName -ErrorAction SilentlyContinue
    if ($existing) {
        $skipped += $vmName
        Write-Host "SKIP $vmName (snapshot exists)" -ForegroundColor Yellow
        continue
    }
    Write-Host "Checkpoint $vmName -> $SnapshotName" -ForegroundColor Cyan
    Checkpoint-VM -Name $vmName -SnapshotName $SnapshotName
    $created += $vmName
}

Write-Host "`nCreated: $($created -join ', ')" -ForegroundColor Green
if ($skipped.Count) {
    Write-Host "Skipped: $($skipped -join ', ')" -ForegroundColor Yellow
}
Write-Host "SnapshotName=$SnapshotName"