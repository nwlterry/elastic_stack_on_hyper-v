param(
    [string]$ConfigPath = (Join-Path (Split-Path $PSScriptRoot) 'config.psd1'),
    [string]$OutputDir  = (Join-Path $PSScriptRoot 'generated')
)

$config = Import-PowerShellDataFile -Path $ConfigPath
$template = Get-Content (Join-Path $PSScriptRoot 'ks.cfg.template') -Raw
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$nameservers = ($config.DnsServers -join ',')
$created = @()

foreach ($node in $config.Nodes) {
    $fqdn = "$($node.Hostname).$($config.Domain)"
    $ks = $template
    $ks = $ks.Replace('{{VMNAME}}', $node.VMName)
    $ks = $ks.Replace('{{FQDN}}', $fqdn)
    $ks = $ks.Replace('{{IP}}', $node.IPAddress)
    $ks = $ks.Replace('{{GATEWAY}}', $config.Gateway)
    $ks = $ks.Replace('{{NAMESERVERS}}', $nameservers)
    $ks = $ks.Replace('{{ROOTPW}}', $config.RootPassword)
    $ks = $ks.Replace('{{TIMEZONE}}', $config.Timezone)

    $nodeDir = Join-Path $OutputDir $node.VMName
    New-Item -ItemType Directory -Force -Path $nodeDir | Out-Null
    $ksPath = Join-Path $nodeDir 'ks.cfg'
    Set-Content -Path $ksPath -Value $ks -Encoding ASCII

    $vhdPath = Join-Path $nodeDir 'OEMDRV.vhdx'
    Dismount-VHD $vhdPath -ErrorAction SilentlyContinue
    if (Test-Path $vhdPath) { Remove-Item $vhdPath -Force }

    # FAT32 requires >= 64 MB
    New-VHD -Path $vhdPath -SizeBytes 64MB -Fixed | Out-Null
    $mounted = Mount-VHD -Path $vhdPath -Passthru
    Start-Sleep -Seconds 3

    $diskNum = $mounted.DiskNumber
    $dpScript = @"
select disk $diskNum
clean
create partition primary
format fs=fat32 label=OEMDRV quick
assign
exit
"@
    $dpFile = Join-Path $env:TEMP "oemdrv_$($node.VMName).txt"
    Set-Content -Path $dpFile -Value $dpScript -Encoding ASCII
    diskpart /s $dpFile | Out-Null
    Start-Sleep -Seconds 2

    $letter = (Get-Partition -DiskNumber $diskNum | Get-Volume).DriveLetter
    if (-not $letter) { throw "No drive letter after format for $($node.VMName)" }
    Copy-Item -Path $ksPath -Destination "${letter}:\ks.cfg" -Force

    Dismount-VHD -Path $vhdPath
    Write-Host "Created OEMDRV VHD for $($node.VMName)" -ForegroundColor Green
    $created += [pscustomobject]@{ VM = $node.VMName; VHD = $vhdPath }
}

$created | Format-Table -AutoSize