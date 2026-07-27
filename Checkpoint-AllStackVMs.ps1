#Requires -Modules Hyper-V
param(
    [string]$SnapshotName = 'pre-upgrade-system-8.18.4-with-apm',
    [string]$LogPath = 'C:\\Users\\terry.ng\\Repository\\elastic_stack_on_hyper-v\\logs\\hv-pre-upgrade-8184-with-apm.log'
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
W "START SnapshotName=$SnapshotName (all stack VMs; keep existing checkpoints)"
foreach ($vmName in $vms) {
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) { W "WARN missing $vmName"; continue }
    $existing = @(Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue)
    W "=== $vmName State=$($vm.State) existing=$($existing.Count) names=$($existing.Name -join ';') ==="
    if ($existing | Where-Object { $_.Name -eq $SnapshotName }) {
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
    $has = $snaps | Where-Object { $_.Name -eq $SnapshotName }
    W "$vmName count=$($snaps.Count) names=$($snaps.Name -join ', ')"
    if (-not $has) { $ok = $false }
}
if ($ok) { W "DONE" } else { W "DONE_WITH_ERRORS"; exit 1 }
exit 0
