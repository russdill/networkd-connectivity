#!/usr/bin/env python3
#
# connectivity-monitord
#
# * Probes Internet reachability *through exactly one interface*.
# * Publishes the enum: (unknown / none / portal / limited / full)
#   as a D-Bus property and emits a signal when it changes.
#
# D-Bus layout  (one object per interface)
# ----------------------------------------
#   Bus name : io.github.russdill.networkd_connectivity.<ifname>
#   Path     : /io/github/russdill/networkd_connectivity/<ifname>
#   IFace    : io.github.russdill.networkd_connectivity1.Device
#       Property  Connectivity  (u)

from __future__ import annotations
import argparse
import asyncio
import errno
import logging
import os
import signal
import socket
import sys
import re
from functools import wraps
from typing import List
import configparser
import subprocess
import collections
import urllib
import sdnotify

import aiohttp
from aiohttp.resolver import AsyncResolver
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property
from dbus_next.constants import BusType, MessageFlag
from dbus_next import Message, MessageType
from dbus_next.errors import DBusError

# Google: # http://gstatic.com/generate_204
# Gnome: http://nmcheck.gnome.org/check_network_status.txt=NetworkManager%20is%20online
# Microsoft: http://www.msftconnecttest.com/connecttest.txt=Microsoft%20Connect%20Test
# Fedora: https://fedoraproject.org/static/hotspot.txt=OK
# Firefox: https://detectportal.firefox.com/success.txt=success
# Apple: https://www.apple.com/library/test/success.html=%3CHTML%3E%3CHEAD%3E%3CTITLE%3ESuccess%3C%2FTITLE%3E%3C%2FHEAD%3E%3CBODY%3ESuccess%3C%2FBODY%3E%3C%2FHTML%3E
# IANA: http://example.com/=...Example%20Domain...

PROBE_URLS = [
    "http://nmcheck.gnome.org/check_network_status.txt=NetworkManager%20is%20online",
    "http://gstatic.com/generate_204",
    "http://example.com/=...Example%20Domain...",
]
DEFAULT_INTERVAL = 15 # seconds
DEFAULT_TIMEOUT = 5   # seconds
CONNECTIVITY     = ["unknown", "none", "portal", "limited", "full"]

def to_base_path(bus_root: str) -> str:
    return "/" + bus_root.replace('.', '/')

BUS_ROOT  = "io.github.russdill.networkd_connectivity"
BASE_PATH = to_base_path(BUS_ROOT)
IFACE_DB  = BUS_ROOT + "1.Device"

NETWORKD_BUS   = "org.freedesktop.network1"
NETWORKD_PATH  = "/org/freedesktop/network1"
RESOLVED_BUS   = "org.freedesktop.resolve1"
RESOLVED_LINK  = "/org/freedesktop/resolve1/link"

def bus_name_for(ifname: str) -> str:
    return BUS_ROOT + '.' + re.sub('[^A-Za-z0-9]', '_', ifname)

def path_for(ifname: str) -> str:
    return BASE_PATH + '/' + re.sub('[^A-Za-z0-9]', '_', ifname)

class ConfigParserMultiValues(collections.OrderedDict):
    def __setitem__(self, key, value):
        if key in self and isinstance(value, list):
            self[key].extend(value)
        else:
            super().__setitem__(key, value)

    @staticmethod
    def getlist(value):
        return value.split(os.linesep)

def load_interface_settings(ifname: str) -> dict:
    proc = subprocess.run(
        ["networkctl", "cat", "@" + ifname],
        capture_output=True, text=True, check=True
    )
    merged = proc.stdout

    # parse only our section
    cp = configparser.ConfigParser(
            strict=False, empty_lines_in_values=False,
            dict_type=ConfigParserMultiValues,
            converters={"list": ConfigParserMultiValues.getlist})
    cp.read_string(merged)
    sec = "ConnectivityMonitord"

    cfg = {}
    if cp.has_option(sec, "ProbeURL"):
        urls = PROBE_URLS[:]
        for part in cp.getlist(sec, "ProbeURL"):
            if not part:
                urls = []
            else:
                urls.append(part)
        cfg["urls"] = urls

    if cp.has_option(sec, "Interval"):
        cfg["interval"] = cp.getfloat(sec, "Interval")

    if cp.has_option(sec, "Timeout"):
        cfg["timeout"] = cp.getfloat(sec, "Timeout")

    return cfg

def bind_all_sockets(ifname: str) -> None:
    # Attempt SO_BINDTODEVICE every new AF_INET/6 socket.
    opt = ifname.encode() + b"\0"
    
    # Test binding immediately to fail fast if permissions are missing.
    # If the interface is missing (ENODEV), we WARN but continue, so the 
    # daemon can survive a temporary device loss and report "down".
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, opt)
    except OSError as e:
        if e.errno == errno.EPERM:
            sys.stderr.write(f"Error: Failed to bind to interface '{ifname}': {e.strerror}\n")
            sys.stderr.write("       (Is the process running as root/CAP_NET_RAW?)\n")
            sys.exit(1)
        elif e.errno == errno.ENODEV:
            logging.warning("Interface '%s' not found (ENODEV); probes will fail until it appears.", ifname)
        else:
            sys.stderr.write(f"Error: Failed to bind to interface '{ifname}': {e.strerror}\n")
            sys.exit(1)

    real = socket.socket

    @wraps(real)
    def factory(*args, **kw):
        s = real(*args, **kw)
        # We only care about AF_INET/AF_INET6 for probing
        if s.family in (socket.AF_INET, socket.AF_INET6):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, opt)
            except OSError as e:
                s.close()
                # If the interface is gone (ENODEV), we re-raise so the probe fails 
                # (counting as 'down/limited') rather than using the default route.
                if e.errno == errno.ENODEV:
                    logging.debug("Failed to bind socket: %s (counting as down)", e)
                    raise
                
                # Other errors (permissions, etc) -> log error and raise
                logging.error("Failed to bind socket to %s: %s", ifname, e)
                raise
        return s
    socket.socket = factory

def dns_bytes_to_ip(family: int, data: Sequence[int]) -> str:
    buf = bytes(data)
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, buf)
    if family == socket.AF_INET6:
        return socket.inet_ntop(socket.AF_INET6, buf)
    raise ValueError

class DeviceStatus(ServiceInterface):
    def __init__(self):
        super().__init__(IFACE_DB)
        self._state = 0 # unknown

    @dbus_property()
    def Connectivity(self) -> "u":
        return self._state

    @Connectivity.setter
    def Connectivity(self, val: "u"):
        if val != self._state:
            self._state = val
            self.emit_properties_changed({"Connectivity": self._state})
            #self.PropertiesChanged(IFACE_DB, {"Connectivity": Variant("u", v)}, [])

async def link_dns(bus: MessageBus, ifindex: int) -> List[str]:
    path = f"{RESOLVED_LINK}/{ifindex}"
    try:
        intro = await bus.introspect(RESOLVED_BUS, path)
    except Exception:
        return []
    obj   = bus.get_proxy_object(RESOLVED_BUS, path, intro)
    props = obj.get_interface("org.freedesktop.DBus.Properties")
    variant = await props.call_get("org.freedesktop.resolve1.Link", "DNS")
    servers = []
    for family, data in variant.value:          # a(iay) to list of structs
        try:
            servers.append(dns_bytes_to_ip(family, data))
        except ValueError:
            continue
    return servers

async def _probe_one(sess: aiohttp.ClientSession, url: str, resp: bytes) -> int:
    try:
        async with sess.get(url, allow_redirects=False) as r:
            if 200 <= r.status < 300:
                data = await r.read()
                if resp.startswith(b'...') and resp.endswith(b'...'):
                    return 4 if resp[3:-3] in data else 2 # ok/portal
                elif resp.startswith(b'...'):
                    return 4 if data.endswith(resp[:-3]) else 2 # ok/portal
                elif resp.endswith(b'...'):
                    return 4 if data.startswith(resp[:-3]) else 2 # ok/portal
                else:
                    return 4 if resp == data else 2 # ok/portal
            elif 300 <= r.status < 400:
                return 2           # portal
            else:
                return 3           # limited
    except aiohttp.ClientConnectorCertificateError:
        return 2                   # portal
    except Exception:
        return 3                   # limited


async def assess(urls: List[tuple], nameservers: List[str], ifname: str, timeout: int = 5) -> int:
    resolver = AsyncResolver(nameservers=nameservers) if nameservers else None
    if resolver:
        try:
            # Bind the c-ares channel to the device.
            # resolver (AsyncResolver) -> _resolver (aiodns.DNSResolver) -> _channel (pycares.Channel)
            resolver._resolver._channel.set_local_dev(ifname.encode())
        except Exception as e:
            logging.warning("Failed to bind DNS resolver to %s: %s", ifname, e)
            return 1

    conn = aiohttp.TCPConnector(ttl_dns_cache=1, resolver=resolver)
    async with aiohttp.ClientSession(
        connector=conn, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as sess:
        res = await asyncio.gather(*(_probe_one(sess, u[0], u[1]) for u in urls))
    return 4 if 4 in res else 2 if 2 in res else 3 if 3 in res else 1

async def run() -> None:
    ap = argparse.ArgumentParser(description="Per-interface connectivity daemon")
    ap.add_argument("interface", metavar="IFACE",
                    help="network interface to probe through")
    ap.add_argument("-i", "--interval", type=float,
                    help="seconds between probe rounds")
    ap.add_argument("-t", "--timeout", type=float,
                    help="timeout in seconds")
    ap.add_argument("-u", "--url", action="append",
                    help="Probe URLs")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    settings = load_interface_settings(args.interface)
    _urls = settings.get("urls", PROBE_URLS)
    interval = settings.get("interval", DEFAULT_INTERVAL)
    timeout = settings.get("timeout", DEFAULT_TIMEOUT)

    if args.interval is not None:
        interval = args.interval
    if args.timeout is not None:
        timeout = args.timeout
    if args.url:
        _urls = args.url

    urls = []
    for url in _urls:
        url, _, resp = url.partition('=')
        resp = urllib.parse.unquote(resp).encode('utf-8')
        urls.append((url, resp))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    bind_all_sockets(args.interface)           # needs CAP_NET_RAW if non-root

    n = sdnotify.SystemdNotifier()

    async def probe_loop():
        while True:
            nameservers = await link_dns(bus, ifindex)
            state = await assess(urls, nameservers, args.interface, timeout)
            if state != status_obj.Connectivity:
                n.notify(f'STATUS={CONNECTIVITY[state]}')
                logging.info("%s: %s -> %s", args.interface,
                             CONNECTIVITY[status_obj.Connectivity],
                             CONNECTIVITY[state])
                status_obj.Connectivity = state
            await asyncio.sleep(interval)

    probe_task: asyncio.Task | None = None

    def maybe_start(state_str: str):
        nonlocal probe_task
        if state_str == "routable":
            if probe_task is None or probe_task.done():
                logging.info("%s: LIMITED (gateway up)", args.interface)
                n.notify(f'STATUS={CONNECTIVITY[3]}')
                status_obj.Connectivity = 3
                probe_task = asyncio.create_task(probe_loop())
        else:
            if probe_task and not probe_task.done():
                probe_task.cancel()
            status_obj.Connectivity = 1
            n.notify(f'STATUS={CONNECTIVITY[1]}')
            logging.info("%s: NONE (not routable)", args.interface)

    # D-Bus setup
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    await bus.request_name(bus_name_for(args.interface))

    status_obj = DeviceStatus()
    bus.export(path_for(args.interface), status_obj)

    call = Message(
        destination=NETWORKD_BUS,
        path=NETWORKD_PATH,
        interface="org.freedesktop.network1.Manager",
        member="GetLinkByName",
        signature="s",
        body=[args.interface],
        flags=MessageFlag.NO_AUTOSTART
    )
    reply = await bus.call(call)
    if reply.message_type == MessageType.ERROR:
        raise DBusError._from_message(reply)
    ifindex, link_path = reply.body

    # Introspect the link object and subscribe via the standard proxy API
    link_intro = await bus.introspect(NETWORKD_BUS, link_path)
    link_obj   = bus.get_proxy_object(NETWORKD_BUS, link_path, link_intro)
    link_props = link_obj.get_interface("org.freedesktop.DBus.Properties")

    def on_props(interface_name: str, changed: dict, invalidated: list):
        # only look at our Link interface
        if interface_name != "org.freedesktop.network1.Link":
            return
        if "OperationalState" in changed:
            maybe_start(changed["OperationalState"].value)

    link_props.on_properties_changed(on_props)

    # And do an initial Get *without* auto-start:
    getmsg = Message(
        destination=NETWORKD_BUS,
        path=link_path,
        interface="org.freedesktop.DBus.Properties",
        member="Get",
        signature="ss",
        body=["org.freedesktop.network1.Link", "OperationalState"],
        flags=MessageFlag.NO_AUTOSTART
    )
    init_reply = await bus.call(getmsg)
    if init_reply.message_type == MessageType.ERROR:
        raise DBusError._from_message(init_reply)
    op_state = init_reply.body[0].value

    # initial
    maybe_start(op_state)

    loop = asyncio.get_running_loop()
    main_task = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    n.notify("READY=1")
    try:
        await main_task
    except asyncio.CancelledError:
        pass

def cli_entry() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    cli_entry()