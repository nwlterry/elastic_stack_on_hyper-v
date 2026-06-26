#!/usr/bin/env python3
"""Print monitoring user and password (never resets elastic password)."""
from deploy_ordered_stack import NODES, connect, get_elastic_password, run
from monitoring_credentials import ensure_monitoring_user, resolve_monitoring_user

if __name__ == "__main__":
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    user, password = ensure_monitoring_user(es, run, elastic_pwd)
    es.close()
    print(f"user={user}")
    print(password)