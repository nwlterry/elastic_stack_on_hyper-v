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

    $ksDir = Join-Path $OutputDir $node.VMName
    New-Item -ItemType Directory -Force -Path $ksDir | Out-Null
    $ksPath = Join-Path $ksDir 'ks.cfg'
    Set-Content -Path $ksPath -Value $ks -Encoding ASCII -NoNewline

    $isoPath = Join-Path $ksDir 'OEMDRV.iso'
    $genScript = Join-Path $PSScriptRoot 'make_oemdrv_iso.py'
    python $genScript $ksPath $isoPath
    $created += [pscustomobject]@{ VM = $node.VMName; Kickstart = $ksPath; Iso = $isoPath }
}

$created | Format-Table -AutoSize
Write-Host "Kickstart ISOs ready in $OutputDir"