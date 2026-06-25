#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run

kb = connect(NODES["kibana"][0])
run(
    kb,
    r"""
python3 <<'PY'
from pathlib import Path
p = Path('/etc/kibana/kibana.yml')
text = p.read_text()
lines = text.splitlines()
out = []
skip = False
for line in lines:
    if line.startswith('xpack.fleet.agentPolicies:'):
        skip = True
        continue
    if skip:
        if line and not line.startswith(' ') and not line.startswith('-'):
            skip = False
        else:
            continue
    out.append(line)
# normalize packages block if duplicated headers only
fixed = []
i = 0
while i < len(out):
    if out[i] == 'xpack.fleet.packages:' and i + 1 < len(out) and out[i+1] == 'xpack.fleet.packages:':
        i += 1
        continue
    fixed.append(out[i])
    i += 1
p.write_text('\n'.join(fixed) + '\n')
print('cleaned kibana.yml')
PY
systemctl reset-failed kibana 2>/dev/null || true
systemctl start kibana
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status 2>/dev/null || echo 000)
  echo "poll $i status=$code"
  [ "$code" = "200" ] && exit 0
  sleep 5
done
exit 1
""",
    timeout=300,
)
print(run(kb, "systemctl is-active kibana", check=False))
kb.close()