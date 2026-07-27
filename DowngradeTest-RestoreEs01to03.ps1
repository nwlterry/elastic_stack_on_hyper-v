#Requires -RunAsAdministrator
#Requires -Modules Hyper-V
param(
    [string]$RestoreSnapshotName = 'pre-upgrade-9.4.1-20260629-1535',
    [string]$Es04CheckpointName = ''
)

$ErrorActionPreference = 'Stop'
if (-not $Es04CheckpointName) {
    $Es04CheckpointName = "pre-downgrade-test-es04-9.4.1-$(Get-Date -Format 'yyyyMMdd-HHmm')"
}

$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'downgrade-test-restore.log'
if (Test-Path $log) { Remove-Item $log -Force }

function W([string]$m) {
    $line = ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m)
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
    Write-Host $line
}

$esTargets = @('ISMELKESNODE01', 'ISMELKESNODE02', 'ISMELKESNODE03')
$es04 = 'ISMELKESNODE04'

try {
    W "START restore_snap=$RestoreSnapshotName es04_ckpt=$Es04CheckpointName"

    $v4 = Get-VM -Name $es04
    W "es04 State=$($v4.State)"
    if (-not (Get-VMSnapshot -VMName $es04 -Name $Es04CheckpointName -EA SilentlyContinue)) {
        W "Checkpoint-VM $es04 ..."
        Checkpoint-VM -Name $es04 -SnapshotName $Es04CheckpointName
        W "es04 checkpoint created"
    } else {
        W "es04 checkpoint already exists"
    }

    foreach ($vm in $esTargets) {
        if (-not (Get-VMSnapshot -VMName $vm -Name $RestoreSnapshotName -EA SilentlyContinue)) {
            throw "Missing snapshot $RestoreSnapshotName on $vm"
        }
        W "OK snap on $vm"
    }

    foreach ($vm in $esTargets) {
        W "Stop-VM $vm"
        Stop-VM -Name $vm -Force -TurnOff -EA SilentlyContinue
    }
    Start-Sleep -Seconds 10

    foreach ($vm in $esTargets) {
        W "Restore-VMSnapshot $vm"
        Restore-VMSnapshot -VMName $vm -Name $RestoreSnapshotName -Confirm:$false
        W "Restored $vm"
    }

    foreach ($vm in $esTargets) {
        W "Start-VM $vm"
        Start-VM -Name $vm
        Start-Sleep -Seconds 10
    }

    $v4 = Get-VM -Name $es04
    if ($v4.State -ne 'Running') {
        W "Start-VM $es04"
        Start-VM -Name $es04
    }

    W "Es04CheckpointName=$Es04CheckpointName"
    W "DONE"
    exit 0
}
catch {
    W "ERROR: $($_.Exception.Message)"
    W "FAIL"
    exit 1
}
