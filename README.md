# python-shared-state-network-service
A p2p Python service that discovers nodes using SSDP and forms a cluster with shared state

This code uses SSDP to discover nodes running the same service and join them to the cluster. PyObjSync is used to create
a cluster that utilizes Raft consensus to share state.
