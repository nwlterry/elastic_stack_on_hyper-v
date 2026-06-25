#Requires -RunAsAdministrator
#Requires -Modules Hyper-V
<#
.SYNOPSIS
    Creates or re-registers 3 Elasticsearch nodes and 1 Kibana node on Hyper-V.

.DESCRIPTION
    - VHDs stored under D:\Virtual Machines\<VMName>\Virtual Hard Disks\
    - Reuses existing OS VHDX if present (ISMELKESNODE01.vhdx pattern)
    - Adds 500 GB data VHDX to each Elasticsearch node
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.psd1'),
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Ensure-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-VMStoragePaths {
    param([string]$Name, [string]$Root)
    $vmDir  = Join-Path $Root $Name
    $vhdDir = Join-Path $vmDir 'Virtual Hard Disks'
    @{
        VMDir  = $vmDir
        VHDDir = $vhdDir
        OSVHD  = Join-Path $vhdDir "$Name.vhdx"
        DataVHD = Join-Path $vhdDir "$Name-Data.vhdx"
    }
}

function New-ClusterVM {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [string]$Name,
        [int]$MemoryGB,
        [int]$ProcessorCount,
        [int]$DataDiskGB = 0,
        [string]$SwitchName,
        [string]$BaseVHD,
        [string]$Root
    )

    $paths   = Get-VMStoragePaths -Name $Name -Root $Root
    $vmDir   = $paths.VMDir
    $vhdDir  = $paths.VHDDir
    $osVhd   = $paths.OSVHD
    $dataVhd = if ($DataDiskGB -gt 0) { $paths.DataVHD } else { $null }

    Ensure-Directory $vmDir
    Ensure-Directory $vhdDir

    if ($PSCmdlet.ShouldProcess($Name, 'Create or update Hyper-V VM')) {
        if (-not (Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue)) {
            throw "Hyper-V switch '$SwitchName' not found. Update VMSwitchName in config.psd1."
        }

        if (-not (Test-Path -LiteralPath $osVhd)) {
            if (Test-Path -LiteralPath $BaseVHD) {
                Write-Host "  Creating differencing OS disk from base image..." -ForegroundColor Yellow
                New-VHD -Path $osVhd -ParentPath $BaseVHD -Differencing | Out-Null
            }
            else {
                throw "OS VHD not found: $osVhd. Install RHEL 8.9 or set BaseVHDPath."
            }
        }
        else {
            Write-Host "  Using existing OS VHD: $osVhd" -ForegroundColor Green
        }

        if ($dataVhd -and -not (Test-Path -LiteralPath $dataVhd)) {
            Write-Host "  Creating ${DataDiskGB} GB data disk: $dataVhd" -ForegroundColor Yellow
            New-VHD -Path $dataVhd -SizeBytes ($DataDiskGB * 1GB) -Dynamic | Out-Null
        }
        elseif ($dataVhd) {
            Write-Host "  Data disk already exists: $dataVhd" -ForegroundColor Green
        }

        $existing = Get-VM -Name $Name -ErrorAction SilentlyContinue
        if (-not $existing) {
            New-VM -Name $Name `
                -MemoryStartupBytes ($MemoryGB * 1GB) `
                -Generation $script:Generation `
                -VHDPath $osVhd `
                -SwitchName $SwitchName `
                -Path $vmDir | Out-Null

            Set-VM -Name $Name -ProcessorCount $ProcessorCount
            Set-VMMemory -VMName $Name -DynamicMemoryEnabled $false
            Set-VMFirmware -VMName $Name -EnableSecureBoot Off -ErrorAction SilentlyContinue
            Enable-VMIntegrationService -VMName $Name -Name 'Guest Service Interface' -ErrorAction SilentlyContinue
        }
        else {
            Write-Warning "VM '$Name' already registered; updating configuration."
            Set-VM -Name $Name -ProcessorCount $ProcessorCount
            Set-VMMemory -VMName $Name -StartupBytes ($MemoryGB * 1GB) -DynamicMemoryEnabled $false
        }

        if ($dataVhd) {
            $hasData = Get-VMHardDiskDrive -VMName $Name | Where-Object { $_.Path -eq $dataVhd }
            if (-not $hasData) {
                Add-VMHardDiskDrive -VMName $Name -Path $dataVhd
                Write-Host "  Attached data disk to $Name" -ForegroundColor Green
            }
        }
    }

    [pscustomobject]@{
        Name     = $Name
        OSVHD    = $osVhd
        DataVHD  = $dataVhd
        MemoryGB = $MemoryGB
        CPUs     = $ProcessorCount
    }
}

$config = Import-PowerShellDataFile -Path $ConfigPath
$script:Generation = $config.Generation

$created = @()

Write-Host "`n=== Elasticsearch nodes (D:\Virtual Machines) ===" -ForegroundColor Cyan
foreach ($vmName in $config.Elasticsearch.Names) {
    $created += New-ClusterVM `
        -Name $vmName `
        -MemoryGB $config.Elasticsearch.MemoryGB `
        -ProcessorCount $config.Elasticsearch.ProcessorCount `
        -DataDiskGB $config.Elasticsearch.DataDiskGB `
        -SwitchName $config.VMSwitchName `
        -BaseVHD $config.BaseVHDPath `
        -Root $config.VMPath
}

Write-Host "`n=== Kibana node ===" -ForegroundColor Cyan
$created += New-ClusterVM `
    -Name $config.Kibana.Name `
    -MemoryGB $config.Kibana.MemoryGB `
    -ProcessorCount $config.Kibana.ProcessorCount `
    -DataDiskGB 0 `
    -SwitchName $config.VMSwitchName `
    -BaseVHD $config.BaseVHDPath `
    -Root $config.VMPath

Write-Host "`n=== VM Summary ===" -ForegroundColor Green
$created | Format-Table Name, MemoryGB, CPUs, OSVHD, DataVHD -AutoSize

Get-VM $created.Name | Format-Table Name, State, @{N='RAM(GB)';E={[math]::Round($_.MemoryStartup/1GB,1)}}, ProcessorCount -AutoSize

Write-Host @"

VHD location: D:\Virtual Machines\<VMName>\Virtual Hard Disks\
  OS:   <VMName>.vhdx
  Data: <VMName>-Data.vhdx  (500 GB, ES nodes only)

Next steps:
1. Start VMs:  Get-VM ISMELK* | Start-VM
2. Copy scripts to each node and run install/bootstrap (see README.md)

"@