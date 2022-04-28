"""Networking utilities."""

import logging
import asyncio
import socket
import struct
import time
import re
import urllib.parse

from . import UPnPError

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
    ]) + '\r\n'

class UPnPInvalidSsdpError(UPnPError): pass
class UPnPInvalidHttpError(UPnPError): pass

# Networking helper functions.
def http_header_as_dict(header):
    """Return the http header as a dict."""

    def normalize(args):
        """Return a normalized (key, value) tuple."""
        return args[0].strip().upper(), args[1].strip()

    # RFC 2616 section 4.2: Header fields can be extended over multiple
    # lines by preceding each extra line with at least one SP or HT.
    compacted = ''
    for line in header:
        sep = '' if not compacted or line.startswith((' ', '\t')) else '\n'
        compacted = sep.join((compacted, line))

    try:
        return dict(normalize(line.split(':', maxsplit=1))
                    for line in compacted.splitlines())
    except ValueError:
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

def parse_ssdp(datagram, ip_source, is_msearch):
    """Return None when ignoring the SSDP, otherwise return a dict."""

    req_line = 'HTTP/1.1 200 OK' if is_msearch else 'NOTIFY * HTTP/1.1'

    # Ignore non 'notify' and non 'msearch' SSDPs.
    try:
       header = datagram.decode().splitlines()
    except UnicodeError:
        return None
    if not header:
        return None
    start_line = header[0].strip()
    if start_line != req_line:
        logger.debug(f"Ignore '{start_line}' request" f' from {ip_source}')
        return None

    # Parse the HTTP header as a dict.
    try:
        header = http_header_as_dict(header[1:])
        check_ssdp_header(header, is_msearch)
    except UPnPInvalidSsdpError as e:
        logger.warning(f'Error from {ip_source}: {e}')
        return None

    # Ignore non root device responses.
    _type = header['ST'] if is_msearch else header['NT']
    if _type != UPNP_ROOTDEVICE:
        return None

    return header

async def msearch(ip, ttl):
    """Implement the SSDP search protocol on the 'ip' network interface.

    Return the list of received (addr, datagram).
    """

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        # Prevent multicast datagrams to be looped back to ourself.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

        # Let the operating system choose the port.
        try:
            sock.bind((ip, 0))
        except OSError as e:
            raise OSError(e.args[0], f'{ip}: {e.args[1]}') from None

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
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

            expire = time.monotonic() + MX

            for i in range(MSEARCH_COUNT):
                await asyncio.sleep(MSEARCH_INTERVAL)
                if not protocol.closed():
                    protocol.m_search(MSEARCH, sock)

            if not protocol.closed():
                remain = expire - time.monotonic()
                if remain > 0:
                    await asyncio.sleep(expire - time.monotonic())

            return  protocol.get_result()
        finally:
            if transport is not None:
                transport.close()
    finally:
        # Needed when OSError is raised upon binding the socket.
        sock.close()

async def notify(ip_addresses, process_datagram):
    """Implement the SSDP advertisement protocol."""

    # See section 21.10 Sending and Receiving in
    # "Network Programming Volume 1, Third Edition" Stevens et al.
    # See also section 5.10.2 Receiving IP Multicast Datagrams
    # in "An Advanced 4.4BSD Interprocess Communication Tutorial".

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        for ip in ip_addresses:
            # Become a member of the IP multicast group on this interface.
            mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GROUP),
                               socket.inet_aton(ip))
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                mreq)
            except OSError as e:
                raise OSError(e.args[0], f'{ip}: {e.args[1]}') from None

        # Allow other processes to bind to the same multicast group and port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind to the multicast (group, port).
        # Binding to (INADDR_ANY, port) would also work, except
        # that in that case the socket would also receive the datagrams
        # destined to (any other address, MCAST_PORT).
        sock.bind(MCAST_ADDR)

        # Start the server.
        transport = None
        try:
            loop = asyncio.get_running_loop()
            on_con_lost = loop.create_future()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: NotifyServerProtocol(process_datagram, on_con_lost),
                sock=sock)
            await on_con_lost
        finally:
            if transport is not None:
                transport.close()
    finally:
        # Needed when OSError is raised upon setting IP_ADD_MEMBERSHIP.
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
        query = query + header + '\r\n' + body
        writer.write(query.encode('latin-1'))

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

        body = await reader.read()

        # Check that we have received the whole body.
        content_length = header_dict.get('CONTENT-LENGTH')
        if content_length is not None:
            content_length = int(content_length)
            if len(body) != content_length:
                raise UPnPInvalidHttpError(f'Content-Length and actual length'
                                f' mismatch ({content_length}-{len(body)})'
                                f' from {host}')
        return header, body

    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()

async def http_get(url):
    """An HTTP 1.0 GET request."""

    header, body = await http_query('GET', url)
    line = header[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', line) is None:
        raise UPnPInvalidHttpError(f'Got "{line}" from {host}')
    return body

async def http_soap(url, header, body):
    """HTTP 1.0 POST request used to submit a SOAP action."""

    header, body = await http_query('POST', url, header, body)
    line = header[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', line) is not None:
        is_fault = False
    elif re.match(r'HTTP/1\.(0|1) 500 ', line) is not None:
        is_fault = True
    else:
        raise UPnPInvalidHttpError(f'Got "{line}" from {host}')
    return is_fault, body

# Network protocols.
class MsearchServerProtocol:
    """The MSEARCH asyncio server."""

    def __init__(self, ip):
        self.ip = ip
        self.transport = None
        self._result = []               # list of received (addr, datagram)
        self._closed = None

    def connection_made(self, transport):
        self.transport = transport
        self._closed = False

    def datagram_received(self, data, addr):
        self._result.append((data, addr))

    def error_received(self, exc):
        logger.warning(f'Error received on {self.ip} by'
                       f' MsearchServerProtocol: {exc}')
        self.transport.abort()

    def connection_lost(self, exc):
        if exc:
            logger.debug(f'Connection lost on {self.ip} by'
                         f' MsearchServerProtocol: {exc!r}')
        self._closed = True

    def m_search(self, message, sock):
        try:
            logger.debug(f'Send mcast M-SEARCH msg to {MCAST_ADDR}'
                         f' on {self.ip} interface')
            self.transport.sendto(message.encode(), MCAST_ADDR)
        except Exception as e:
            self.error_received(e)

    def get_result(self):
        return self._result

    def closed(self):
        return self._closed

class NotifyServerProtocol:
    """The NOTIFY asyncio server."""

    def __init__(self ,process_datagram, on_con_lost):
        self.process_datagram = process_datagram
        self.on_con_lost = on_con_lost
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.process_datagram(data, addr[0], False)
        except Exception as exc:
            self.error_received(exc)

    def error_received(self, exc):
        logger.error(f'Error received by NotifyServerProtocol: {exc!r}')
        self.transport.abort()

    def connection_lost(self, exc):
        if not self.on_con_lost.done():
            self.on_con_lost.set_result(True)
        if exc:
            logger.debug(f'Connection lost by NotifyServerProtocol: {exc!r}')
