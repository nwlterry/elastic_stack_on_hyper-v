#!/usr/bin/env python3
import os, paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=os.environ["SSH_PASS"], timeout=30)
cmds = [
    "grep -E '^(node.name|cluster.initial_master_nodes|discovery.seed_hosts)' /etc/elasticsearch/elasticsearch.yml",
    "sed -n '115,130p' /etc/elasticsearch/elasticsearch.yml",
    "curl -sk -u 'elastic:60=csJudh5OQc63qdpDw' 'https://localhost:9200/_cluster/health?pretty' 2>&1",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    print(f"=== {cmd} ===")
    print((o.read() + e.read()).decode())
c.close()