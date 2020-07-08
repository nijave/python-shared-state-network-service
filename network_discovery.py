import asyncio
import logging
import queue
import socket
import threading
import uuid
from typing import Dict

import ssdp
import util

SSDP_PORT = 1900
IPV4_LINK_LOCAL = "239.255.255.250"
IPV6_LINK_LOCAL = "ff02::c"
IPV6_SITE_LOCAL = "ff05::c"
IPV6_ORG_LOCAL = "ff08::c"
IPV6_GLOBAL = "ff0e::c"
SSDP_BROADCAST_ADDRESSES = {
    socket.AF_INET: IPV4_LINK_LOCAL,
    socket.AF_INET6: IPV6_LINK_LOCAL,
}

# ssdp unique client identifier
CLIENT_ID = uuid.uuid4()


class SimpleNetworkService(ssdp.SimpleServiceDiscoveryProtocol):
    """
    Handles SSDP events

    See https://williamboles.me/discovering-whats-out-there-with-ssdp/ for high-level overview
    & https://github.com/codingjoe/ssdp/tree/master/examples for basic examples
    """
    SEARCH_TARGET = "urn:schemas-upnp-org:service:PythonSimpleStateShare:1"

    def __init__(self, peers: queue.Queue = None):
        """
        :param peers: a set to be populated with discovered peers
        """
        self._logger = logging.getLogger(self.__class__.__name__)
        self._logger.debug("Created new protocol handler")
        self.transport = None
        self.peers = peers if peers is not None else queue.Queue()
        super().__init__()

    # override/extend
    def connection_made(self, transport: asyncio.transports.BaseTransport) -> None:
        self.transport = transport
        super().connection_made(transport)

    # implement
    def response_received(self, response: ssdp.SSDPResponse, addr: tuple) -> None:
        self._logger.info("Response: %r from %s", response, addr)
        self._logger.debug(util.obj_dumper(response))
        headers = dict(response.headers)
        self.peers.put(headers["Location"])

    # implement
    def request_received(self, request: ssdp.SSDPRequest, addr: tuple) -> None:
        self._logger.info("Request: %r from %s", request, addr)
        self._logger.debug(util.obj_dumper(request))

        headers = dict(request.headers)
        self._logger.debug("Headers: %s", headers)
        if headers.get("MAN", "").strip('"') == "ssdp:discover":
            return self._discover(headers, addr)
        else:
            self._logger.warning("Unknown message type %s", headers["MAN"])

    def _discover(self, headers: Dict[str, any], addr: tuple) -> None:
        self._logger.info("Got discovery request for %s from %s", headers["ST"], addr)
        if headers["ST"] not in ("ssdp:all", self.__class__.SEARCH_TARGET):
            self._logger.info("Skipping reply for unknown service %s", headers["ST"])
            return

        r = ssdp.SSDPResponse(
            200,
            "OK",
            headers={
                "Cache-Control": "max-age=30",
                "Location": util.current_sync_host(),
                "Server": f"Python UPnP/1.0 {self.__class__.__name__}",
                "ST": self.__class__.SEARCH_TARGET,
                "USN": f"uuid:{CLIENT_ID}::{self.__class__.SEARCH_TARGET}",
                "EXT": "",
            },
        )
        self.transport.sendto(bytes(r) + b'\r\n', addr)


class SsdpCommunicator:
    """
    Handles node discovery.
    Acts are both client and server
    """

    def __init__(
            self,
            loop: asyncio.AbstractEventLoop,
            discovered_peers: queue.Queue,
            shutdown_event: threading.Event,
    ):
        self._loop = loop
        self._discovered_peers = discovered_peers
        self._should_stop = shutdown_event
        self._logger = logging.getLogger(self.__class__.__name__)

    @staticmethod
    def _ssdp_socket(*, proto: socket.AddressFamily = socket.AF_INET):
        """
        Creates a socket for SSDP communications.
        See https://github.com/python/asyncio/issues/480#issuecomment-324253075 for socket setup details
        :param proto: AF_INET for IPv4 or AF_INET6 for IPv6
        :return: socket for sending/receiving messages/broadcasts
        """

        if proto not in SSDP_BROADCAST_ADDRESSES:
            raise ValueError("Invalid protocol supplied")
        listen = SSDP_BROADCAST_ADDRESSES[proto]

        sock_level = {
            socket.AF_INET: socket.IPPROTO_IP,
            socket.AF_INET6: socket.IPPROTO_IPV6,
        }

        sock_opt = {
            socket.AF_INET: socket.IP_ADD_MEMBERSHIP,
            socket.AF_INET6: socket.IPV6_JOIN_GROUP,
        }

        default_ip = {
            socket.AF_INET: "0.0.0.0",
            socket.AF_INET6: "::",
        }

        sock = socket.socket(proto, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        sock.bind(('', SSDP_PORT))
        sock.setsockopt(
            sock_level[proto],
            sock_opt[proto],
            b"".join([socket.inet_pton(proto, ip) for ip in (listen, default_ip[proto])])
        )

        return sock

    @staticmethod
    def _transport_to_broadcast_addr(transport: asyncio.transports.BaseTransport):
        """
        Returns the correct broadcast address given a transport
        :param transport: asyncio transport
        :return:  tuple (ip, port) to send broadcasts to
        """
        return SSDP_BROADCAST_ADDRESSES[
                   transport.get_extra_info('socket').family
               ], SSDP_PORT

    async def start(self):
        """
        Runs peer discovery
        :param loop: async loop to client/server inside of
        :return: set of peers including this one
        """
        service = SimpleNetworkService(peers=self._discovered_peers)

        # Setup ipv4 socket
        transports = [
            self._loop.create_datagram_endpoint(
                lambda: service,
                sock=self._ssdp_socket(proto=socket.AF_INET),
            )
        ]

        # Setup ipv6 socket--might fail if the system doesn't support ipv6
        try:
            transports.append(self._loop.create_datagram_endpoint(
                lambda: service,
                sock=self._ssdp_socket(proto=socket.AF_INET6)
            ))
        except OSError:
            self._logger.error("IPv6 not supported. Skipping.")

        transports = [
            (await s)[0]
            for s in transports
        ]

        # logger.debug("Sleeping before discovery")
        # await asyncio.sleep(5)

        # Create a search request
        search_request = ssdp.SSDPRequest(
            "M-SEARCH",
            headers={
                "HOST": IPV4_LINK_LOCAL,
                "MAN": '"ssdp:discover"',
                "MX": 5,
                # "ST": "ssdp:all",
                "ST": SimpleNetworkService.SEARCH_TARGET,
                "SERVER": socket.gethostname(),
            },
        )

        sleep_time = 10
        # try to find something a few times
        while not self._should_stop.is_set():
            self._logger.info("Performing M-SEARCH request")
            # Send out an M-SEARCH request, requesting all service types.
            for t in transports:
                search_request.sendto(t, self._transport_to_broadcast_addr(t))
            await asyncio.sleep(sleep_time)

        for t in transports:
            t.close()
