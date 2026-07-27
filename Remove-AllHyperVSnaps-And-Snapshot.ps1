#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Delete ALL Hyper-V checkpoints on ELK VMs, then create a fresh named checkpoint.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\config.psd1',
    [string]$SnapshotName = "post-roles-data-content-$(Get-Date -Format 'yyyyMMdd-HHmm')",
    [string]$LogPath = 'C:\Users\terry.ng\Repository\elastic_stack_on_hyper-v\logs\hv-snap-reset.log'
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
function W([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

W "START SnapshotName=$SnapshotName ConfigPath=$ConfigPath"
$vms = @(
    'ISMELKESNODE01','ISMELKESNODE02','ISMELKESNODE03','ISMELKESNODE04',
    'ISMELKKBNNODE01','ISMELKFLNODE01'
)
try {
    if (Test-Path -LiteralPath $ConfigPath) {
        $config = Import-PowerShellDataFile -Path $ConfigPath
        $fromCfg = @($config.Nodes | ForEach-Object { $_.VMName } | Where-Object { $_ })
        if ($fromCfg.Count -gt 0) { $vms = $fromCfg }
        else { W "WARN config Nodes empty; using hardcoded VM list" }
    } else {
        W "WARN config missing: $ConfigPath; using hardcoded VM list"
    }
} catch {
    W "WARN config import failed: $_; using hardcoded VM list"
}
W "VMs: $($vms -join ', ')"

foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        W "WARN missing VM $vmName"
        continue
    }
    W "=== $vmName State=$($vm.State) ==="
    # Remove root snapshots (cascades children)
    $roots = Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue |
        Where-Object { -not $_.ParentSnapshotId }
    if (-not $roots) {
        # some trees only list via Get-VMSnapshot without parent filter
        $all = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
        if ($all.Count -eq 0) {
            W "  no snapshots"
        } else {
            foreach ($s in ($all | Sort-Object CreationTime)) {
                W "  remove $($s.Name) ($($s.CreationTime))"
                try {
                    Remove-VMSnapshot -VMName $vmName -Name $s.Name -IncludeAllChildSnapshots -ErrorAction Stop
                } catch {
                    W "  remove failed $($s.Name): $_"
                }
            }
        }
    } else {
        foreach ($s in $roots) {
            W "  remove root $($s.Name) (+children) ($($s.CreationTime))"
            try {
                Remove-VMSnapshot -VMName $vmName -Name $s.Name -IncludeAllChildSnapshots -ErrorAction Stop
            } catch {
                W "  remove failed $($s.Name): $_"
            }
        }
    }
    # verify empty
    $left = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    W "  remaining snapshots: $($left.Count)"
    if ($left.Count -gt 0) {
        foreach ($s in $left) {
            W "  FORCE remove $($s.Name)"
            Remove-VMSnapshot -VMName $vmName -Name $s.Name -IncludeAllChildSnapshots -ErrorAction SilentlyContinue
        }
        $left = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
        W "  remaining after force: $($left.Count)"
    }
}

W "=== Create checkpoints: $SnapshotName ==="
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) { continue }
    W "Checkpoint-VM $vmName -> $SnapshotName"
    try {
        Checkpoint-VM -Name $vmName -SnapshotName $SnapshotName -ErrorAction Stop
        W "  OK $vmName"
    } catch {
        W "  FAIL $vmName : $_"
        throw
    }
}

W "=== VERIFY ==="
foreach ($vmName in $vms) {
    $snaps = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    $names = ($snaps | ForEach-Object { $_.Name }) -join '; '
    W "$vmName count=$($snaps.Count) names=$names"
}

W "SnapshotName=$SnapshotName"
W "DONE"
