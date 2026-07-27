#Requires -RunAsAdministrator
#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Create ISMELKESNODE04 (flash RHEL install) and attach a 200 GB NFS data disk to Kibana.

.DESCRIPTION
  - Generates kickstart OEMDRV for es04 only
  - Creates OS + 500 GB data VHDX, boots flash install from RHEL ISO
  - Creates ISMELKKBNNODE01-NFS.vhdx (200 GB), attaches to running/stopped Kibana
  Does NOT recreate existing ES/Kibana/Fleet VMs.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.psd1'),
    [int]$NfsDiskGB = 200,
    [switch]$SkipOsInstall,
    [switch]$NfsDiskOnly,
    [switch]$Es04Only
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$config = Import-PowerShellDataFile -Path $ConfigPath
$es04 = $config.Nodes | Where-Object { $_.VMName -eq 'ISMELKESNODE04' }
if (-not $es04) { throw 'ISMELKESNODE04 missing from config.psd1 Nodes' }

$kbn = $config.Nodes | Where-Object { $_.VMName -eq 'ISMELKKBNNODE01' }
if (-not $kbn) { throw 'ISMELKKBNNODE01 missing from config.psd1 Nodes' }

function Get-NodePaths {
    param([string]$VMName)
    $vmDir = Join-Path $config.VMPath $VMName
    $vhdDir = Join-Path $vmDir 'Virtual Hard Disks'
    @{
        VMDir   = $vmDir
        VHDDir  = $vhdDir
        OSVHD   = Join-Path $vhdDir "$VMName.vhdx"
        DataVHD = Join-Path $vhdDir "$VMName-Data.vhdx"
        NfsVHD  = Join-Path $vhdDir "$VMName-NFS.vhdx"
        OemVhd  = Join-Path $PSScriptRoot "kickstart\generated\$VMName\OEMDRV.vhdx"
    }
}

function New-OemDrvForNode {
    param($Node)
    $oemScript = Join-Path $PSScriptRoot 'kickstart\New-OemDrvVhd.ps1'
    # Temporary single-node config for kickstart generator
    $tmp = Join-Path $env:TEMP "config-es04-only.psd1"
    $single = $config.Clone()
    # Import-PowerShellDataFile returns hashtable; rebuild minimal
    $nodesBlock = @"
@{
    VMSwitchName = '$($config.VMSwitchName)'
    VMPath = '$($config.VMPath)'
    VHDPath = '$($config.VHDPath)'
    Generation = $($config.Generation)
    RHELDvdIso = '$($config.RHELDvdIso)'
    RHELVersion = '$($config.RHELVersion)'
    RootPassword = '$($config.RootPassword)'
    Domain = '$($config.Domain)'
    Gateway = '$($config.Gateway)'
    DnsServers = @($(($config.DnsServers | ForEach-Object { "'$_'" }) -join ', '))
    Timezone = '$($config.Timezone)'
    ClusterName = '$($config.ClusterName)'
    ElasticVersion = '$($config.ElasticVersion)'
    Nodes = @(
        @{
            VMName = '$($Node.VMName)'
            Hostname = '$($Node.Hostname)'
            IPAddress = '$($Node.IPAddress)'
            Role = '$($Node.Role)'
            MemoryGB = $($Node.MemoryGB)
            ProcessorCount = $($Node.ProcessorCount)
            OSDiskGB = $($Node.OSDiskGB)
            DataDiskGB = $($Node.DataDiskGB)
        }
    )
    FleetServerPolicyName = '$($config.FleetServerPolicyName)'
    EsAgentPolicyName = '$($config.EsAgentPolicyName)'
    KibanaAgentPolicyName = '$($config.KibanaAgentPolicyName)'
}
"@
    Set-Content -Path $tmp -Value $nodesBlock -Encoding UTF8
    & $oemScript -ConfigPath $tmp
}

function New-Es04FlashVm {
    param($Node)
    if (-not (Test-Path $config.RHELDvdIso)) {
        throw "RHEL ISO not found: $($config.RHELDvdIso)"
    }
    $paths = Get-NodePaths -VMName $Node.VMName
    New-Item -ItemType Directory -Force -Path $paths.VHDDir | Out-Null

    New-OemDrvForNode -Node $Node
    if (-not (Test-Path $paths.OemVhd)) {
        throw "Kickstart VHD missing: $($paths.OemVhd)"
    }

    if (-not (Test-Path $paths.OSVHD)) {
        Write-Host "Creating OS disk $($paths.OSVHD) ($($Node.OSDiskGB) GB)" -ForegroundColor Cyan
        New-VHD -Path $paths.OSVHD -SizeBytes ($Node.OSDiskGB * 1GB) -Dynamic | Out-Null
    }
    if ($Node.DataDiskGB -gt 0 -and -not (Test-Path $paths.DataVHD)) {
        Write-Host "Creating data disk $($paths.DataVHD) ($($Node.DataDiskGB) GB)" -ForegroundColor Cyan
        New-VHD -Path $paths.DataVHD -SizeBytes ($Node.DataDiskGB * 1GB) -Dynamic | Out-Null
    }

    $existing = Get-VM -Name $Node.VMName -ErrorAction SilentlyContinue
    if (-not $existing) {
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
        Write-Host "Created VM $($Node.VMName)" -ForegroundColor Green
    }
    else {
        Write-Host "VM $($Node.VMName) already exists" -ForegroundColor Yellow
    }

    # Detach data during OS install
    Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object {
        $_.Path -like '*-Data*' -or $_.Path -eq $paths.DataVHD
    } | ForEach-Object {
        Remove-VMHardDiskDrive -VMName $Node.VMName `
            -ControllerType $_.ControllerType -ControllerNumber $_.ControllerNumber `
            -ControllerLocation $_.ControllerLocation
    }

    Get-VMDvdDrive -VMName $Node.VMName | Remove-VMDvdDrive -ErrorAction SilentlyContinue
    Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object { $_.Path -like '*OEMDRV*' } | ForEach-Object {
        Remove-VMHardDiskDrive -VMName $Node.VMName `
            -ControllerType $_.ControllerType -ControllerNumber $_.ControllerNumber `
            -ControllerLocation $_.ControllerLocation
    }
    Add-VMDvdDrive -VMName $Node.VMName -Path $config.RHELDvdIso
    $hasOem = Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object { $_.Path -eq $paths.OemVhd }
    if (-not $hasOem) { Add-VMHardDiskDrive -VMName $Node.VMName -Path $paths.OemVhd }

    $boot = (Get-VMFirmware -VMName $Node.VMName).BootOrder
    $dvdEntry = $boot | Where-Object { $_.Device -eq 'DVD' } | Select-Object -First 1
    if ($dvdEntry) {
        $newOrder = @($dvdEntry) + ($boot | Where-Object { $_.Device -ne 'DVD' })
        Set-VMFirmware -VMName $Node.VMName -BootOrder $newOrder
    }

    if (-not $SkipOsInstall) {
        if ((Get-VM $Node.VMName).State -ne 'Running') {
            Start-VM -Name $Node.VMName
            Write-Host "Started $($Node.VMName) - flash install in progress (15-40 min)" -ForegroundColor Green
        }
        else {
            Write-Host "$($Node.VMName) already running" -ForegroundColor Yellow
        }
    }

    [pscustomobject]@{
        VM = $Node.VMName
        IP = $Node.IPAddress
        OSVHD = $paths.OSVHD
        DataVHD = $paths.DataVHD
    }
}

function Add-KibanaNfsDisk {
    param($Node, [int]$SizeGB)
    $paths = Get-NodePaths -VMName $Node.VMName
    New-Item -ItemType Directory -Force -Path $paths.VHDDir | Out-Null

    if (-not (Test-Path $paths.NfsVHD)) {
        Write-Host "Creating NFS disk $($paths.NfsVHD) (${SizeGB} GB)" -ForegroundColor Cyan
        New-VHD -Path $paths.NfsVHD -SizeBytes ($SizeGB * 1GB) -Dynamic | Out-Null
    }
    else {
        Write-Host "NFS disk already exists: $($paths.NfsVHD)" -ForegroundColor Green
    }

    if (-not (Get-VM -Name $Node.VMName -ErrorAction SilentlyContinue)) {
        throw "Kibana VM $($Node.VMName) not found"
    }

    $attached = Get-VMHardDiskDrive -VMName $Node.VMName | Where-Object { $_.Path -eq $paths.NfsVHD }
    if (-not $attached) {
        Add-VMHardDiskDrive -VMName $Node.VMName -Path $paths.NfsVHD
        Write-Host "Attached NFS disk to $($Node.VMName)" -ForegroundColor Green
    }
    else {
        Write-Host "NFS disk already attached to $($Node.VMName)" -ForegroundColor Green
    }

    [pscustomobject]@{
        VM = $Node.VMName
        NfsVHD = $paths.NfsVHD
        SizeGB = $SizeGB
    }
}

Write-Host "`n=== Add ES04 + Kibana NFS disk ===" -ForegroundColor Cyan
$results = @()
if (-not $NfsDiskOnly) {
    $results += New-Es04FlashVm -Node $es04
}
if (-not $Es04Only) {
    $results += Add-KibanaNfsDisk -Node $kbn -SizeGB $NfsDiskGB
}
$results | Format-List

Write-Host @"

Next:
  1. Wait for es04 flash install: ssh root@$($es04.IPAddress)  (marker: /root/.flash-install-complete)
  2. Attach ES data disk post-install (if needed): Attach-DataDisks.ps1 or setup script
  3. Run: python setup_nfs_snapshot_roles.py

"@
