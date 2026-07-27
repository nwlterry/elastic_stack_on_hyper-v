#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Rename Hyper-V checkpoints on all ELK VMs to a fixed name.
#>
[CmdletBinding()]
param(
    [string]$OldName = 'post-roles-data-content-20260724-1245',
    [string]$NewName = 'pre-upgrade-system-8.18.4-20260724',
    [string]$LogPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\logs\hv-rename-snap.log'
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

$vms = @(
    'ISMELKESNODE01','ISMELKESNODE02','ISMELKESNODE03','ISMELKESNODE04',
    'ISMELKKBNNODE01','ISMELKFLNODE01'
)

W "START rename OldName=$OldName NewName=$NewName"
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        W "WARN missing VM $vmName"
        continue
    }
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    W "=== $vmName snaps=$($snaps.Count) names=$($snaps.Name -join ';') ==="
    $target = $snaps | Where-Object { $_.Name -eq $OldName } | Select-Object -First 1
    if (-not $target) {
        # If already renamed, or single snap with different name, rename any non-matching
        $already = $snaps | Where-Object { $_.Name -eq $NewName } | Select-Object -First 1
        if ($already) {
            W "  already named $NewName"
            continue
        }
        if ($snaps.Count -eq 1) {
            $target = $snaps[0]
            W "  old name not found; renaming sole snapshot '$($target.Name)'"
        } else {
            W "  ERROR no snapshot matching OldName and not a single snap"
            continue
        }
    }
    try {
        Rename-VMSnapshot -VMSnapshot $target -NewName $NewName -ErrorAction Stop
        W "  OK renamed -> $NewName"
    } catch {
        W "  ERROR rename: $_"
    }
}

W "=== VERIFY ==="
$ok = $true
foreach ($vmName in $vms) {
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    $names = $snaps.Name -join ','
    W "$vmName count=$($snaps.Count) names=$names"
    if ($snaps.Count -ne 1 -or $snaps[0].Name -ne $NewName) { $ok = $false }
}
if ($ok) { W "DONE" } else { W "DONE_WITH_ERRORS"; exit 1 }
exit 0
