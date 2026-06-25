#Requires -Modules Hyper-V
<#
.SYNOPSIS
    Flash-install RHEL 8.10 on ELK stack VMs — creates VHDs, VMs, unattended OS from DVD ISO.

.DESCRIPTION
    Folder layout:
      D:\Virtual Machines\<VMName>\<VMName>.xml
      D:\Virtual Machines\<VMName>\Virtual Hard Disks\<VMName>.vhdx
      D:\Virtual Machines\<VMName>\Virtual Hard Disks\<VMName>-Data.vhdx  (ES nodes)

.PARAMETER Recreate
    Remove existing ISMELK* VMs and recreate VHDs from scratch.

.PARAMETER SkipOsInstall
    Only create/register VMs; do not start OS installer.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ConfigPath,
    [switch]$Recreate,
    [switch]$SkipOsInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ConfigPath) { $ConfigPath = Join-Path $ScriptRoot 'config.psd1' }

$config = Import-PowerShellDataFile -Path $ConfigPath

if (-not (Test-Path $config.RHELDvdIso)) {
    throw "RHEL ISO not found: $($config.RHELDvdIso)"
}

# Generate kickstart OEMDRV VHDs if missing (FAT32 + ks.cfg — reliable on Hyper-V Gen2)
$needKs = $config.Nodes | Where-Object {
    -not (Test-Path (Join-Path $ScriptRoot "kickstart\generated\$($_.VMName)\OEMDRV.vhdx"))
}
if ($needKs) {
    & (Join-Path $ScriptRoot 'kickstart\New-OemDrvVhd.ps1') -ConfigPath $ConfigPath
}

function Get-NodePaths {
    param([string]$VMName)
    $vmDir  = Join-Path $config.VMPath $VMName
    $vhdDir = Join-Path $vmDir 'Virtual Hard Disks'
    @{
        VMDir   = $vmDir
        VHDDir  = $vhdDir
        OSVHD   = Join-Path $vhdDir "$VMName.vhdx"
        DataVHD = Join-Path $vhdDir "$VMName-Data.vhdx"
        OemVhd  = Join-Path $ScriptRoot "kickstart\generated\$VMName\OEMDRV.vhdx"
    }
}

function Remove-ElkVm {
    param([string]$Name)
    if (Get-VM -Name $Name -ErrorAction SilentlyContinue) {
        if ((Get-VM $Name).State -eq 'Running') { Stop-VM $Name -Force -TurnOff }
        Remove-VM $Name -Force
        Write-Host "Removed VM $Name" -ForegroundColor Yellow
    }
}

function New-FlashVm {
    param($Node)

    $paths = Get-NodePaths -VMName $Node.VMName
    New-Item -ItemType Directory -Force -Path $paths.VHDDir | Out-Null

    if ($Recreate) {
        Remove-ElkVm -Name $Node.VMName
        Remove-Item -Path $paths.OSVHD -Force -ErrorAction SilentlyContinue
        if ($Node.DataDiskGB -gt 0) {
            Remove-Item -Path $paths.DataVHD -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-Path $paths.OSVHD)) {
        Write-Host "Creating OS disk $($paths.OSVHD) ($($Node.OSDiskGB) GB)" -ForegroundColor Cyan
        New-VHD -Path $paths.OSVHD -SizeBytes ($Node.OSDiskGB * 1GB) -Dynamic | Out-Null
    }

    # Data disks created but NOT attached during OS install (attached post-install)
    if ($Node.DataDiskGB -gt 0 -and -not (Test-Path $paths.DataVHD)) {
        Write-Host "Creating data disk $($paths.DataVHD) ($($Node.DataDiskGB) GB) [attach after OS]" -ForegroundColor Cyan
        New-VHD -Path $paths.DataVHD -SizeBytes ($Node.DataDiskGB * 1GB) -Dynamic | Out-Null
    }

    if (-not (Test-Path $paths.OemVhd)) {
        throw "Kickstart VHD missing: $($paths.OemVhd)"
    }

    if (-not (Get-VM -Name $Node.VMName -ErrorAction SilentlyContinue)) {
        New-VM -Name $Node.VMName `
            -MemoryStartupBytes ($Node.MemoryGB * 1GB) `
            -Generation $config.Generation `
            -VHDPath $paths.OSVHD `
            -SwitchName $config.VMSwitchName `
            -Path $paths.VMDir | Out-Null

        Set-VM -Name $Node.VMName -ProcessorCount $Node.ProcessorCount
        Set-VMMemory -VMName $Node.VMName -DynamicMemoryEnabled $false
        Set-VMFirmware -VMName $Node.VMName -EnableSecureBoot Off
        Enable-VMIntegrationService -VMName $Node.VMName -Name 'Guest Service Interface' -ErrorAction SilentlyContinue
    }

    # Detach data disks during OS install (attached post-install via Attach-DataDisks.ps1)
    if ($Node.DataDiskGB -gt 0) {
        Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object {
            $_.Path -like '*-Data*' -or $_.Path -eq $paths.DataVHD
        } | ForEach-Object {
            Remove-VMHardDiskDrive -VMName $Node.VMName `
                -ControllerType $_.ControllerType -ControllerNumber $_.ControllerNumber `
                -ControllerLocation $_.ControllerLocation
            Write-Host "Detached data disk from $($Node.VMName) for OS install" -ForegroundColor Yellow
        }
    }

    # DVD: RHEL installer; OEMDRV kickstart VHD on SCSI (auto-detected by anaconda)
    Get-VMDvdDrive -VMName $Node.VMName | Remove-VMDvdDrive -ErrorAction SilentlyContinue
    Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object { $_.Path -like '*OEMDRV*' } | ForEach-Object {
        Remove-VMHardDiskDrive -VMName $Node.VMName -ControllerType $_.ControllerType -ControllerNumber $_.ControllerNumber -ControllerLocation $_.ControllerLocation
    }
    Add-VMDvdDrive -VMName $Node.VMName -Path $config.RHELDvdIso
    $hasOem = Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object { $_.Path -eq $paths.OemVhd }
    if (-not $hasOem) { Add-VMHardDiskDrive -VMName $Node.VMName -Path $paths.OemVhd }

    # Boot from DVD first (RHEL installer)
    $boot = (Get-VMFirmware -VMName $Node.VMName).BootOrder
    $dvdEntry = $boot | Where-Object { $_.Device -eq 'DVD' } | Select-Object -First 1
    if ($dvdEntry) {
        $newOrder = @($dvdEntry) + ($boot | Where-Object { $_.Device -ne 'DVD' })
        Set-VMFirmware -VMName $Node.VMName -BootOrder $newOrder
    }

    if (-not $SkipOsInstall) {
        if ((Get-VM $Node.VMName).State -ne 'Running') {
            Start-VM -Name $Node.VMName
            Write-Host "Started $($Node.VMName) - RHEL 8.10 flash install in progress" -ForegroundColor Green
        }
    }

    [pscustomobject]@{
        VM       = $Node.VMName
        IP       = $Node.IPAddress
        OSVHD    = $paths.OSVHD
        DataVHD  = if ($Node.DataDiskGB -gt 0) { $paths.DataVHD } else { '' }
        Kickstart = $paths.OemVhd
    }
}

Write-Host "`n=== Flash ELK Stack - RHEL $($config.RHELVersion) ===" -ForegroundColor Cyan
$summary = @()
foreach ($node in $config.Nodes) {
    $summary += New-FlashVm -Node $node
}

$summary | Format-Table VM, IP, OSVHD, DataVHD -AutoSize

Write-Host @"

OS install running on all VMs (15–40 min each).
Monitor:  ssh root@<ip>  (password in config.psd1)
Verify:   test -f /root/.flash-install-complete && echo OK

When all nodes respond on SSH, run:
  `$env:SSH_PASS = '<root-password>'
  python "$ScriptRoot\deploy_full_stack.py"

"@