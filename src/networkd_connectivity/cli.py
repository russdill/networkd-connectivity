#!/usr/bin/env python3
#
# connectivity-status
# Query the D-Bus service created by connectivity-monitord instances and
# print a one-line summary per interface.
#
# Output:
#     IFACE      STATE_NUM  STATE_NAME
#     eth0       4          full
#     wlan0      2          portal

import argparse
import asyncio
import re
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

# constants that match connectivity-monitord
def to_base_path(bus_root: str) -> str:
    return "/" + bus_root.replace('.', '/')

BUS_ROOT  = "io.github.russdill.networkd_connectivity"
BASE_PATH = to_base_path(BUS_ROOT)
IFACE_DB  = BUS_ROOT + "1.Device"
STATES     = ["unknown", "none", "portal", "limited", "full"]

async def fetch_state(bus, path: str) -> int:
    intro = await bus.introspect(BUS_ROOT, path)
    proxy = bus.get_proxy_object(BUS_ROOT, path, intro)
    props = proxy.get_interface("org.freedesktop.DBus.Properties")
    variant = await props.call_get(IFACE_DB, "Connectivity") # uint32
    return variant.value

async def main(show_header: bool) -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    introspection = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    dbus_obj = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspection)
    dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")
    names = await dbus_iface.call_list_names()
    svc = [m.group(0, 1) for m in (re.match(rf'^{BUS_ROOT}\.([a-zA-Z0-9_]+)$', n) for n in names) if m] 

    if show_header:
        print(f"{'IFACE':10} {'#':<3} STATE")

    for name, ifname in svc:
        path = f"{BASE_PATH}/{ifname}"
        intro = await bus.introspect(name, path)
        proxy = bus.get_proxy_object(name, path, intro)
        props = proxy.get_interface("org.freedesktop.DBus.Properties")
        var = await props.call_get(IFACE_DB, "Connectivity")
        num = var.value
        print(f"{ifname:10} {num:<3}  {STATES[num]}")

def cli_entry() -> None:
    parser = argparse.ArgumentParser(
        description="Display current connectivity status per interface")
    parser.add_argument("-H", "--no-header", action="store_true",
                        help="suppress header row")
    args = parser.parse_args()

    try:
        asyncio.run(main(not args.no_header))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    cli_entry()
