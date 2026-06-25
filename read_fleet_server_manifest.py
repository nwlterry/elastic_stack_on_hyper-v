#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run

kb = connect(NODES["kibana"][0])
print(run(kb, r"""python3 <<'PY'
import zipfile
z = zipfile.ZipFile('/usr/share/kibana/node_modules/@kbn/fleet-plugin/target/bundled_packages/fleet_server-1.6.0.zip')
for name in z.namelist():
    if 'agent.yml' in name or 'input' in name.lower():
        print('===', name, '===')
        print(z.read(name).decode()[:2000])
PY
""", timeout=60))
kb.close()