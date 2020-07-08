import asyncio
import concurrent
import logging
import queue
import socket
import threading
import time
from concurrent.futures._base import Future
from datetime import datetime
from random import randint

from pysyncobj import SyncObj, SyncObjConf
from pysyncobj.batteries import ReplDict

import util

import network_discovery

logging.basicConfig(
    format=f'%(asctime)s:%(levelname)8s:{socket.gethostname():>32s}:%(process)8d:%(name)s: %(message)s',
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def ssdp_handler(*, peers_queue: queue.Queue, shutdown_event: threading.Event) -> None:
    """
    Starts SSDP client/server to handle node discovery and populates
    `peers_queue` with discovered nodes. Nodes are continually
    re-discovered so the queue may provide previously discovered
    nodes
    :param peers_queue: connection strings for discovered nodes
    :param shutdown_event: event that signals thread should stop
    :return:
    """
    _loop = asyncio.new_event_loop()
    communicator = network_discovery.SsdpCommunicator(
        loop=_loop,
        discovered_peers=peers_queue,
        shutdown_event=shutdown_event,
    )
    _loop.run_until_complete(communicator.start())


def peer_handler(
    *,
    peers_set: util.ExpiringSet,
    peers_queue: queue.Queue,
    sync_obj: SyncObj,
    shutdown_event: threading.Event,
    discovery_event: threading.Event,
    discovery_timeout: int = 15
) -> None:
    """
    Updates PySyncObj with peer changes from SSDP discovery server.

    This method will first perform a "discovery" mode will it will listen for
    peers before performing any updates. This allows initial cluster bootstrapping to complete successfully
    and allows new nodes time to notify existing cluster and discover existing nodes before joining.
    :param peers_set: set of known peers. This will be updated in-place as new peers are discovered
    :param peers_queue: queue of peer connection strings (discovered by SSDP)
    :param sync_obj: PySyncObj cluster that will have new peers dynamically added
    :param shutdown_event: event that signals when thread should stop
    :param discovery_event: threading.Event that gets signaled once initial discovery is complete
    :param discovery_timeout: time to wait in discovery mode before making cluster updates
    :return: None
    """
    _current_host = util.current_sync_host()
    start_time = time.time()
    while not shutdown_event.is_set():
        try:
            peer = peers_queue.get()
        except queue.Empty:
            time.sleep(0.05)
            continue
        # The SSDP code will discover itself but
        # PyObjSync treats `self` differently
        if peer == _current_host:
            continue
        else:
            if start_time <= (time.time()-discovery_timeout):
                logger.debug("Discovery timeout met")
                discovery_event.set()
            if peer not in peers_set and discovery_event.is_set():
                logger.info("Dynamically adding peer %s", peer)
                try:
                    sync_obj.result(timeout=0).addNodeToCluster(peer)
                except concurrent.futures.TimeoutError:
                    pass
            # Refresh ttl regardless
            peers_set.add(peer)


shutdown = threading.Event()
q = queue.Queue()

ssdp_server = threading.Thread(
    target=ssdp_handler,
    kwargs=dict(
        peers_queue=q,
        shutdown_event=shutdown,
    )
)
ssdp_server.start()

current_host = util.current_sync_host()
peers = util.ExpiringSet()

logger.info("Discovering initial nodes")

# Cluster relies on an initial node-set and cluster updates
# rely on the existence of the cluster. This future allows
# the regular discovery code to work before the cluster has
# been initially created
s_future = concurrent.futures.Future()
discovery_complete = threading.Event()
node_manager = threading.Thread(
    target=peer_handler,
    kwargs=dict(
        peers_set=peers,
        peers_queue=q,
        sync_obj=s_future,
        discovery_event=discovery_complete,
        shutdown_event=shutdown,
    )
)
node_manager.start()
discovery_complete.wait()
logger.info("Initial discovery complete, Found peers %s", peers)

d = ReplDict()
s = SyncObj(
    selfNode=current_host,
    otherNodes=peers,
    consumers=[d],
    conf=SyncObjConf(dynamicMembershipChange=True)
)
s_future.set_result(s)


for _ in range(600):
    logger.info(s.getStatus())

    d.set(current_host, datetime.now().isoformat(), sync=True)
    d.set("shared_key", datetime.now().isoformat() + " " + current_host, sync=True)
    time.sleep(randint(0, 50) / 10)
    for k, v in d.items():
        logger.info("%10s: %s", k, v)
