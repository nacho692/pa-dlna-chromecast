"""Networking utilities."""

import asyncio
import socket
import struct
import time
import re
import logging
import urllib.parse
import psutil
from ipaddress import IPv4Interface, IPv4Address

from . import UPnPError, TEST_LOGLEVEL

logger = logging.getLogger('network')

MCAST_GROUP = '239.255.255.250'
MCAST_PORT = 1900
MCAST_ADDR = (MCAST_GROUP, MCAST_PORT)
UPNP_ROOTDEVICE = 'upnp:rootdevice'

MSEARCH_COUNT= 3                        # number of MSEARCH requests each time
MSEARCH_INTERVAL = 0.2                  # sent at seconds intervals
MX = 2                                  # seconds to delay response

MSEARCH = '\r\n'.join([
        f'M-SEARCH * HTTP/1.1',
        f'HOST: {MCAST_GROUP}:{MCAST_PORT}',
        f'MAN: "ssdp:discover"',
        f'ST: {UPNP_ROOTDEVICE}',
        f'MX: {MX}',
        f'',
        f'',
        ])

class UPnPInvalidSsdpError(UPnPError): pass
class UPnPInvalidHttpError(UPnPError): pass

# Networking helper functions.
def ipv4_addresses(ifaces, yield_str=True):
    """Yield IPv4 addresses on 'ifaces' network interface cards.

    Use all existing network interface cards when 'ifaces' is empty.
    """

    nics = psutil.net_if_addrs()
    for nic in filter(lambda x: not ifaces or x in ifaces, nics):
        for addr in filter(lambda x: x.family == socket.AF_INET, nics[nic]):
            if addr.netmask is not None:
                ipadd = IPv4Interface(f'{addr.address}/{addr.netmask}')
                if ipadd.network.prefixlen != 32:
                    yield addr.address if yield_str else ipadd
            else:
                yield addr.address if yield_str else IPv4Address(addr.address)

def http_header_as_dict(header):
    """Return the http header as a dict."""

    def normalize(args):
        """Return a normalized (key, value) tuple."""
        return args[0].strip().upper(), args[1].strip()

    # RFC 2616 (obsoleted) section 4.2: Header fields can be extended over
    # multiple lines by preceding each extra line with at least one SP or HT.
    # But see RFC 7230 section 3.2.4: A server that receives an obs-fold ...
    # [may] replace each received obs-fold with one or more SP octets.
    compacted = ''
    for line in header:
        sep = '' if not compacted or line.startswith((' ', '\t')) else '\n'
        compacted = sep.join((compacted, line))

    try:
        return dict(normalize(line.split(':', maxsplit=1))
                    for line in compacted.splitlines())
    except (ValueError, IndexError):
        raise UPnPInvalidSsdpError(f'malformed HTTP header:\n{header}')

def check_ssdp_header(header, is_msearch):
    """Check the SSDP header."""

    def exist(keys):
        for key in keys:
            if key not in header:
                raise UPnPInvalidSsdpError(
                    f'missing "{key}" field in SSDP notify:\n{header}')

    # Check the presence of some required keys.
    if is_msearch:
        exist(('ST', 'LOCATION', 'USN'))
    else:
        exist(('NT', 'NTS', 'USN'))
        if header['NTS'] in ('ssdp:alive', 'ssdp:update'):
            exist(('LOCATION',))

def parse_ssdp(datagram, peer_ipaddress, is_msearch):
    """Return None when ignoring the SSDP, otherwise return a dict."""

    req_line = 'HTTP/1.1 200 OK' if is_msearch else 'NOTIFY * HTTP/1.1'

    # Ignore non 'notify' and non 'msearch' SSDPs.
    header = datagram.decode().splitlines()
    start_line = header[:1]
    if not start_line or start_line[0].strip() != req_line:
        if start_line:
            logger.log(TEST_LOGLEVEL,
                       f"Ignore '{start_line[0].strip()}' request")
        return None

    # Parse the HTTP header as a dict.
    try:
        header = http_header_as_dict(header[1:])
        check_ssdp_header(header, is_msearch)
    except UPnPInvalidSsdpError as e:
        logger.warning(f'Error from {peer_ipaddress}: {e}')
        return None

    # Ignore non root device responses.
    _type = header['ST'] if is_msearch else header['NT']
    if _type != UPNP_ROOTDEVICE:
        logger.log(TEST_LOGLEVEL, f"Ignore '{_type}': non root device")
        return None

    return header

async def msearch(ip, protocol, msearch_count=MSEARCH_COUNT,
                  msearch_interval=MSEARCH_INTERVAL, mx=MX):
    """Implement the SSDP search protocol on the 'ip' network interface.

    Return the list of received (data, peer_addr, local_addr).
    """

    expire = time.monotonic() + mx

    for i in range(msearch_count):
        await asyncio.sleep(msearch_interval)
        if not protocol.closed():
            protocol.send_datagram(MSEARCH)
        else:
            break
    logger.debug(f'Sent {i + 1} M-SEARCH datagrams to {MCAST_ADDR} from {ip}')

    if not protocol.closed():
        remain = expire - time.monotonic()
        if remain > 0:
            await asyncio.sleep(expire - time.monotonic())

    return  protocol.get_result()

async def send_mcast(ip, port=None, ttl=2, coro=msearch):
    """Send multicast datagrams.

    'coro' is a coroutine *function* and when invoked, the coroutine is
    awaited with the 'protocol' end point as parameter for sending and
    receiving datagrams.
    """

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        # Prevent multicast datagrams to be looped back to ourself.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

        try:
            # Let the operating system choose the port.
            if port is None:
                port = 0
            sock.bind((ip, port))
        except OSError as e:
            # Just log the exception, the associated network interface may
            # be reconnected later.
            logger.debug(f'Cannot bind to IP address {ip}: {e!r}')
            return

        # Start the server.
        transport = None
        try:
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: MsearchServerProtocol(ip), sock=sock)

            # Prepare the socket for sending from the network
            # interface of 'ip'.
            sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                            ttl)

            return await coro(ip, protocol)

        finally:
            if transport is not None:
                transport.close()
    finally:
        # Needed when OSError is raised upon binding the socket.
        sock.close()

async def http_query(method, url, header='', body=''):
    """An HTTP 1.0 GET or POST request."""

    assert method in ('GET', 'POST')
    writer = None
    try:
        url = urllib.parse.urlsplit(url)
        host = url.hostname
        port = url.port if url.port is not None else 80
        reader, writer = await asyncio.open_connection(host, port)

        # Send the request.
        request = url._replace(scheme='')._replace(netloc='').geturl()
        query = (
            f"{method} {request or '/'} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
        )
        query = query + header + '\r\n'
        writer.write(query.encode('latin-1'))
        writer.write(body.encode())

        # Parse the http header.
        header = []
        while True:
            line = await reader.readline()
            if not line:
                break

            line = line.decode('latin1').rstrip()
            if line:
                header.append(line)
            else:
                break

        if not header:
            raise UPnPInvalidHttpError(f'Empty http header from {host}')

        header_dict = http_header_as_dict(header[1:])
        content_length = header_dict.get('CONTENT-LENGTH')
        if content_length is not None:
            content_length = int(content_length)
            if content_length == 0:
                return header, b'', host

        body = await reader.read()

        # Check that we have received the whole body.
        if content_length is not None:
            if len(body) != content_length:
                raise UPnPInvalidHttpError(f'Content-Length and actual length'
                                f' mismatch ({content_length} != {len(body)})'
                                f' from {host}')
        return header, body, host

    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()

async def http_get(url):
    """An HTTP 1.0 GET request."""

    header, body, host = await http_query('GET', url)
    line = header[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', line) is None:
        raise UPnPInvalidHttpError(f"Header={header}, Body={body}"
                                   f" from {host}")
    return body

async def http_soap(url, header, body):
    """HTTP 1.0 POST request used to submit a SOAP action."""

    header, body, host = await http_query('POST', url, header, body)
    line = header[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', line) is not None:
        is_fault = False
    # HTTP/1.0 500 Internal Server Error.
    elif re.match(r'HTTP/1\.(0|1) 500 ', line) is not None:
        is_fault = True
    else:
        raise UPnPInvalidHttpError(f"Header={header}, Body={body}"
                                   f" from {host}")
    return is_fault, body

# Classes.
class Notify:
    """Implement the SSDP advertisement protocol.

    See section 21.10 Sending and Receiving in
    "Network Programming Volume 1, Third Edition" Stevens et al.
    See also section 5.10.2 Receiving IP Multicast Datagrams
    in "An Advanced 4.4BSD Interprocess Communication Tutorial".
    """

    def __init__(self, process_datagram, ip_addresses):
        self.process_datagram = process_datagram

        # Create the socket.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

        self.ip_addresses = set()
        self.manage_membership(ip_addresses)

        # Future used by the test suite.
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def manage_membership(self, ip_addresses):
        def member(ip, optname):
            msg = ('member of' if optname == socket.IP_ADD_MEMBERSHIP else
                   'dropped from')
            try:
                mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GROUP),
                                   socket.inet_aton(ip))
                self.sock.setsockopt(socket.IPPROTO_IP, optname, mreq)
                logger.debug(f'SSDP notify: {ip} {msg} multicast group'
                             f' {MCAST_GROUP}')
            except OSError as e:
                logger.warning(f'SSDP notify: {ip} cannot be {msg}'
                               f' {MCAST_GROUP}: {e.args[0]}')
                return False
            return True

        for ip in ip_addresses.difference(self.ip_addresses):
            if member(ip, socket.IP_ADD_MEMBERSHIP):
                self.ip_addresses.add(ip)

        for ip in self.ip_addresses.difference(ip_addresses):
            if member(ip, socket.IP_DROP_MEMBERSHIP):
                self.ip_addresses.remove(ip)

    async def run(self):
        # Allow other processes to bind to the same multicast group and port.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind to the multicast (group, port).
        # Binding to (INADDR_ANY, port) would also work, except
        # that in that case the socket would also receive the datagrams
        # destined to (any other address, MCAST_PORT).
        self.sock.bind(MCAST_ADDR)

        # Start the server.
        transport = None
        try:
            loop = asyncio.get_running_loop()
            on_con_lost = loop.create_future()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: NotifyServerProtocol(self.process_datagram,
                                             on_con_lost),
                sock=self.sock)
            self.startup.set_result(None)
            await on_con_lost
            logger.debug("Future 'on_con_lost' is done.")
        finally:
            # Drop multicast group membership for all IP addresses.
            self.manage_membership(set())
            if transport is not None:
                transport.close()
            logger.info('End of the SSDP notify task')

# Network protocols.
class MsearchServerProtocol:
    """The MSEARCH asyncio server."""

    def __init__(self, ip):
        self.ip = ip
        self.transport = None
        self._result = []     # list of received (data, peer_addr, local_addr)
        self._closed = None

    def connection_made(self, transport):
        self.transport = transport
        self._closed = False

    def datagram_received(self, data, peer_addr):
        local_addr = self.transport.get_extra_info('sockname')
        self._result.append((data, peer_addr[0], local_addr[0]))

    def error_received(self, exc):
        logger.warning(f'Error received on {self.ip} by'
                       f' MsearchServerProtocol: {exc}')
        self.transport.abort()

    def connection_lost(self, exc):
        if exc:
            logger.debug(f'Connection lost on {self.ip} by'
                         f' MsearchServerProtocol: {exc!r}')
        self._closed = True

    def send_datagram(self, message):
        try:
            self.transport.sendto(message.encode(), MCAST_ADDR)
        except Exception as e:
            self.error_received(e)

    def get_result(self):
        return self._result

    def closed(self):
        return self._closed

class NotifyServerProtocol:
    """The NOTIFY asyncio server."""

    def __init__(self, process_datagram, on_con_lost):
        self.process_datagram = process_datagram
        self.on_con_lost = on_con_lost

    def connection_made(self, transport):
        pass

    def datagram_received(self, data, addr):
        try:
            self.process_datagram(data, addr[0], None)
        except Exception as exc:
            if not self.on_con_lost.done():
                self.on_con_lost.set_result(True)
            self.error_received(exc)

    def error_received(self, exc):
        logger.error(f'Error received by NotifyServerProtocol: {exc!r}')

    def connection_lost(self, exc):
        if exc:
            logger.warning(f'Connection lost by NotifyServerProtocol: {exc!r}')
