#!/usr/bin/env python3
"""One-time elastic password reset; saves to secrets/elastic-password and ES01."""
from deploy_ordered_stack import NODES, connect, reset_elastic_password, run

if __name__ == "__main__":
    c = connect(NODES["es01"][0])
    password = reset_elastic_password(c)
    c.close()
    print(password)