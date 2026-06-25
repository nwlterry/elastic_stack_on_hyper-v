# Attach 500GB data disks to ES nodes after OS install
$ErrorActionPreference = 'Stop'

function Read-ConfigPsd1 {
    param([string]$Path)
    $text = Get-Content -Path $Path -Raw
    $script = [scriptblock]::Create($text)
    return & $script
}

$config = Read-ConfigPsd1 (Join-Path $PSScriptRoot 'config.psd1')

foreach ($node in $config.Nodes) {
    if ($node.DataDiskGB -le 0) { continue }
    $dataVhd = Join-Path $config.VMPath "$($node.VMName)\Virtual Hard Disks\$($node.VMName)-Data.vhdx"
    if (-not (Test-Path $dataVhd)) { Write-Warning "Missing $dataVhd"; continue }
    $attached = Get-VMHardDiskDrive -VMName $node.VMName | Where-Object {
        $_.Path -eq $dataVhd -or $_.Path -like "*$($node.VMName)-Data*"
    }
    if (-not $attached) {
        Add-VMHardDiskDrive -VMName $node.VMName -Path $dataVhd
        Write-Host "Attached data disk to $($node.VMName)" -ForegroundColor Green
    }
}