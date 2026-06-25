#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run
kb = connect(NODES["kibana"][0])
print(run(kb, "command -v docker; docker --version 2>/dev/null || echo NO_DOCKER; free -h | head -2", check=False))
kb.close()