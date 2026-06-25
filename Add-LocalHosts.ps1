# Add ELK stack FQDNs to the Windows hosts file for local name resolution.
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.psd1')
)

$ErrorActionPreference = 'Stop'

function Read-ConfigPsd1 {
    param([string]$Path)
    $script = [scriptblock]::Create((Get-Content -Path $Path -Raw))
    return & $script
}

$config = Read-ConfigPsd1 $ConfigPath
$hostsPath = "$env:Windir\System32\drivers\etc\hosts"
$markerStart = '# BEGIN ISM-ELK-CLUSTER'
$markerEnd   = '# END ISM-ELK-CLUSTER'

$lines = @($markerStart)
foreach ($node in $config.Nodes) {
    $fqdn = "$($node.Hostname).$($config.Domain)"
    $short = $node.Hostname
    $ip = $node.IPAddress
    $lines += ('{0}    {1}    {2}' -f $ip, $fqdn, $short)
}
$lines += $markerEnd

$content = Get-Content $hostsPath -Raw
if ($content -match [regex]::Escape($markerStart)) {
    $pattern = "(?s)$([regex]::Escape($markerStart)).*?$([regex]::Escape($markerEnd))"
    $content = [regex]::Replace($content, $pattern, ($lines -join "`r`n"))
} else {
    $content = $content.TrimEnd() + "`r`n`r`n" + ($lines -join "`r`n") + "`r`n"
}

$tmp = Join-Path $env:TEMP "hosts.elk.$PID"
Set-Content -Path $tmp -Value $content -Encoding ASCII -Force
Move-Item -Path $tmp -Destination $hostsPath -Force
Write-Host "Updated $hostsPath with $($config.Nodes.Count) ELK node records:" -ForegroundColor Green
$lines | Where-Object { $_ -notmatch '^# ' } | ForEach-Object { Write-Host "  $_" }