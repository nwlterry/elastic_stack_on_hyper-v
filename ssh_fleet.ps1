$cfg = Import-PowerShellDataFile -Path (Join-Path $PSScriptRoot 'config.psd1')
$pass = $cfg.RootPassword
$env:SSH_PASS = $pass
# Use plink if available, else ssh with key workaround via python one-liner
python -u -c @"
import paramiko, time, re
from pathlib import Path
cfg = Path('config.psd1').read_text()
pwd = re.search(r\"RootPassword\s*=\s*'([^']+)'\", cfg).group(1)
for attempt in range(10):
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect('10.44.40.42', username='root', password=pwd, timeout=20, banner_timeout=60)
        for cmd in ['uptime','systemctl is-active sshd','ps aux | grep elastic | grep -v grep | wc -l','ss -tn state established | grep :22 | wc -l']:
            _,o,e = c.exec_command(cmd, timeout=20)
            print(cmd+':', (o.read()+e.read()).decode().strip())
        c.close()
        break
    except Exception as ex:
        print(f'attempt {attempt}: {ex}')
        time.sleep(5)
"@