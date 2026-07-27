#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

c = connect(NODES["es01"][0])
auth = curl_elastic_auth(get_elastic_password(c))
print(
    run(
        c,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'; "
        f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'; "
        f"curl -sk -u {auth} -X POST 'https://localhost:9200/_snapshot/fs_nfs_snapshots/_verify?pretty'; "
        f"curl -sk -u {auth} 'https://localhost:9200/_snapshot/fs_nfs_snapshots/_all?pretty' | head -c 800",
        check=False,
    )
)
c.close()
for name in ("es01", "es02", "es03", "es04"):
    cn = connect(NODES[name][0])
    print(
        f"=== {name} ===",
        run(
            cn,
            "id elasticsearch; getent group elasticsearch; "
            "mountpoint -q /mnt/es-snapshots && df -h /mnt/es-snapshots | tail -1; "
            "grep -E 'path.repo|node.roles|cluster.name' /etc/elasticsearch/elasticsearch.yml; "
            "rpm -q elasticsearch",
            check=False,
        ),
    )
    cn.close()
