.. _galera:

==================
Mysql Galera Guide
==================

This guide provides an overview of Galera implementation in CCP.

Overview
~~~~~~~~

Galera Cluster is a synchronous multi-master database cluster, based on
synchronous replication and MySQL/InnoDB. When Galera Cluster is in use, you
can direct reads and writes to any node, and you can lose any individual node
without interruption in operations and without the need to handle complex
failover procedures.

CCP implementaion details
~~~~~~~~~~~~~~~~~~~~~~~~~

Entrypoint script
-----------------

To handle all required logic, CCP has a dedicated entrypoint script for
Galera and its side-containers. Because of that, Galera pods are slightly
different from the rest of CCP pods. For example, Galera container still uses
CCP global entrypoint, but it executes Galera entrypoint, which is executing
MySQL and handles all required logic, like bootstrapping, fail detection, etc.

Galera pod
----------

Each Galera pod consists of 3 containers:

* galera
* galera-checker
* galera-haproxy

**galera** - a container which runs Galera itself.

**galera-checker** - a container with galera-checker script. It is used to
check readiness and liveness of the Galera node.

**galera-haproxy** - a container with a haproxy instance.

.. NOTE:: More info about each container is available in the
  "Galera containers" section.

Etcd usage
----------

The current implementation uses etcd to store cluster state. The default etcd
root the directory will be ``/galera/k8scluster``.

Additional keys and directories are:

* **leader** - key with the IP address of the current leader. Leader - is just
  a single, random Galera node, which haproxy will be used as a backend.
* **nodes/** - directory with current Galera nodes. Each node key will be
  named as an IP address of the node and value will be a Unix time of the key
  creation.
* **queue/** - directory with current Galera nodes waiting in the recovery
  queue. This is needed to ensure that all nodes are ready, before looking for
  the node with the highest seqno. Each node key will be named as an IP addr
  of the node and value will be a Unix time of the key creation.
* **seqno/** - directory with current Galera nodes seqno's.
  Each node key will be named as an IP address of the node and its value will
  be a seqno of the node's data.
* **state** - key with current cluster state. Can be "STEADY", "BUILDING" or
  "RECOVERY"
* **uuid** - key with current uuid of the Galera cluster. If a new node will
  have a different uuid, this will indicate that we have a split brain
  situation. Nodes with the wrong uuid will be destroyed.

Galera containers
~~~~~~~~~~~~~~~~~

galera
------

This container runs Galera daemon, plus handles all the bootstrapping,
reconnecting and recovery logic.

At the start of the container, it checks for the ``init.ok`` file in the Galera
data directory. If this file doesn't exist, it removes all files from the
data directory, running Mysql init, to create base mysql data files, after
we're starting mysqld daemon without networking and setting needed permissions
for expected users.

If ``init.ok`` file is found, it runs the ``mysqld_safe --wsrep-recover``
to recover Galera related information and write it to the ``grastate.dat``
file.

After that, it checks the cluster state and depending on the current state
it chose required scenario.

galera-checker
--------------

This container is used for liveness and readiness checks of Galera pod.

To check if this Galera pod is ready it checks for the following things:

#. wsrep_local_state_comment = "Synced"
#. wsrep_evs_state = "OPERATIONAL"
#. wsrep_connected = "ON"
#. wsrep_ready = "ON"
#. wsrep_cluster_state_uuid = uuid in the etcd

To check if this Galera pod is alive we checking the following things:

#. If current cluster state is not "STEADY" - it skips liveness check.
#. If it detects that SST sync is in progress - it skips liveness check.
#. If it detects that there is no Mysql pid file yet - it skips liveness
   check.
#. If node "wsrep_cluster_state_uuid" differs from the etcd one - it kills
   Galera container, since it's a "split brain" situation.
#. If "wsrep_local_state_comment" is "Joined", and the previous state was
   "Joined" too - it kills Galera container since it can't finish joining
   to the cluster for some reason.
#. If it caught any exception during the checks - it kills Galera container.

If all checks passed - we're deciding that Galera pod is alive.

galera-haproxy
--------------

This container is used to run haproxy daemon, which is used to send all traffic
to a single Galera pod.

This is needed to avoid deadlocks and stale reads. It chooses the "leader"
out of all available Galera pods and once leader is chosen, all haproxy
instances update their configuration with the new leader.

Supported scenarios
~~~~~~~~~~~~~~~~~~~

Initial bootstrap
-----------------

In this scenario, there is no working Galera cluster yet. Each node trying to
get the lock in etcd, first one which can start cluster bootstrapping. After
it's done, next node gets the lock and connects to the existing cluster.

.. NOTE:: During the bootstrap state of the cluster will be "BUILDING". It will
  be changed to "STEADY" after last node connection.

Re-connecting to the existing cluster
-------------------------------------

In this scenario, Galera cluster is already available. In most case it will be
a node re-connection after some failures, such as node reboot. Each node tries
to get the lock in etcd, once lock acquiring node connects to the existing
cluster.

.. NOTE:: During this scenario state of the cluster will be "STEADY".

Recovery
--------

This scenario could be triggered by two possible options:

* Operator manually sets cluster state in etcd to the "RECOVERY"
* New node does a few checks before bootstrapping, if it finds that cluster
  state is "STEADY", but there is zero nodes in the cluster - it assumes that
  cluster has been destroyed somehow and we need to run recovery. In that case,
  it sets the state to the "RECOVERY" and starts recovery scenario.

During the recovery scenario cluster bootstrapping is different from the
"Initial bootstrap". In this scenario, each node looks for its "seqno", which
is basically the registered number of the transactions. A node with the highest
seqno will bootstrap cluster and other nodes will join it, so in the end, we
will have the latest data available before the cluster destruction.

.. NOTE:: During the bootstrap state of the cluster will be "RECOVERY". It will
  be changed to "STEADY" after last node connection.

There is an option to manually choose the node to recover data from.
For details please see the "force bootstrap" section in the "Advanced features"
.

Advanced features
~~~~~~~~~~~~~~~~~

Cluster size
------------

By default, galera cluster size will be 3 nodes. This is optimal for the most
cases. If you want to change it to some custom number, you need to override
**cluster_size** variable in the **percona** tree, for example:

::

    configs:
      percona:
        cluster_size: 5

.. NOTE:: Cluster size should be an odd number. Cluster size with more that 5
  nodes will lead to big latency for write operations.

Force bootstrap
---------------

Sometimes operators may want to manually specify Galera node which recovery
should be done from. In that case, you need to override **force_bootstrap**
variable in the **percona** tree, for example:

::

    configs:
      percona:
        force_bootstrap:
          enabled: true
          node: NODE_NAME

**NODE_NAME** should be the name of the k8s node, which will run Galera node
with required data.

Troubleshooting
~~~~~~~~~~~~~~~

Galera operation requires some advanced knowledge in Mysql and in some general
clustering conceptions. In most cases, we expect that Galera will "self-heal"
itself, in the worst case via restart, full resync and reconnection to the
cluster.

Our readiness and liveness scripts should cover this, and not allow
misconfigured or non-operational node receive production traffic.

Yet it's possible that some failure scenarios is not covered and to fix them
some manual actions could be required.

Check the logs
--------------

Each container of the Galera pod writes detailed logs to the stdout. You could
read them via ``kubectl logs POD_NAME -c CONT_NAME``. Make sure you check the
``galera`` container logs and ``galera-checker`` ones.

Additionally you should check the Mysql logs in the
``/var/log/ccp/mysql/mysql.log``

Check the etcd state
--------------------

Galera keeps its state in the etcd and it could be useful to check what is
going on in the etcd right now. Assuming that you're using the **ccp**
namespace, you could check etcd state using this command:

::

    etcdctl --endpoints http://etcd.ccp.svc.cluster.local:2379 ls -r -p --sort /galera
    etcdctl --endpoints http://etcd.ccp.svc.cluster.local:2379 get /galera/k8scluster/state
    etcdctl --endpoints http://etcd.ccp.svc.cluster.local:2379 get /galera/k8scluster/leader
    etcdctl --endpoints http://etcd.ccp.svc.cluster.local:2379 get /galera/k8scluster/uuid

Node restart
------------

In most cases, it should be safe to restart a single Galera node. If you need
to do it for some reason, just delete the pod, via kubectl:

::

    kubectl delete pod POD_NAME

Full cluster restart
--------------------

In some cases, you may need to restart the whole cluster. Make sure you have a
backup before doing this. To do this, set the cluster state to the "RECOVERY":

::

    etcdctl --endpoints http://etcd.ccp.svc.cluster.local:2379 set /galera/k8scluster/state RECOVERY

After that restart all Galera pods:

::

    kubectl delete pod POD1_NAME POD2_NAME POD3_NAME

Once that done, Galera cluster will be rebuilt and should be operational.

.. NOTE:: For more info about cluster recovery please refer to the
  "Supported scenarios" section.
