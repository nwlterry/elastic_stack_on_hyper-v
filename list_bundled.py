#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run
kb = connect(NODES["kibana"][0])
print(run(kb, "ls /usr/share/kibana/node_modules/@kbn/fleet-plugin/target/bundled_packages/"))
kb.close()