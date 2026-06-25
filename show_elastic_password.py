#!/usr/bin/env python3
"""Print the current elastic password (never resets)."""
from deploy_ordered_stack import NODES, connect, get_elastic_password

if __name__ == "__main__":
    c = connect(NODES["es01"][0])
    print(get_elastic_password(c))
    c.close()