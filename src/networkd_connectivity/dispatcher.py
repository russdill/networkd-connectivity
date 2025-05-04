#!/usr/bin/env python3
# connectivity-dispatcher
#
# Execute user hooks when a networkd-connectivity instance changes state.
#
# Within each root, hooks reside in  <state>.d/  sub-directories,
# e.g.  full.d/00-refresh  portal.d/20-alert   none.d/90-backoff
#
# Execution order: **lexicographically ascending** (0-z).  
# The script receives no positional arguments; the environment contains
#
#    IFACE=<interface name>
#    STATE=<state name>

import argparse, asyncio, logging, os, pathlib, re
import signal
import subprocess
import sdnotify
from typing import List

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

def to_base_path(bus_root: str) -> str:
    return "/" + bus_root.replace('.', '/')

BUS_ROOT  = "io.github.russdill.networkd_connectivity"
BASE_PATH = to_base_path(BUS_ROOT)
IFACE_DB  = BUS_ROOT + "1.Device"
STATE_ENUM = ["unknown", "none", "portal", "limited", "full"]
PATH_RE   = re.compile(rf"^{BASE_PATH}/([A-Za-z0-9_]+)$")

def run_hooks(roots: List[pathlib.Path], state: str, iface: str):
    env = os.environ.copy()
    env["IFACE"] = iface
    env["STATE"] = state
    logging.debug("run_hooks IFACE=%s STATE=%s", iface, state)
    for root in roots:
        dir_ = root / f"{state}.d"
        if not dir_.is_dir():
            continue
        for script in sorted(dir_.iterdir()):
            if not os.access(script, os.X_OK):
                continue
            logging.debug("exec %s (IFACE=%s STATE=%s)", script, iface, state)
            try:
                subprocess.Popen([str(script)], env=env)
            except Exception as exc:
                logging.error("failed to run %s: %s", script, exc)

async def main() -> None:
    ap = argparse.ArgumentParser(description="connectivity hook dispatcher")
    ap.add_argument("-S", "--script-dir",
                    help="colon-separated list of hook roots",
                    default="/usr/lib/connectivity-dispatcher:/etc/connectivity-dispatcher")
    ap.add_argument("-T", "--run-startup-triggers", action="store_true",
                    help="invoke hooks once for the current state on start")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    roots = [pathlib.Path(p) for p in args.script_dir.split(":")]
    logging.info("hook search path: %s", ":".join(str(r) for r in roots))

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def attach(ifname: str, path: str, bus_name: str, run_initial: bool):
        intro = await bus.introspect(bus_name, path)
        obj   = bus.get_proxy_object(bus_name, path, intro)
        iface = obj.get_interface(IFACE_DB)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        logging.debug("attach IFACE=%s", ifname)

        if run_initial:
            num = await iface.get_connectivity()
            run_hooks(roots, STATE_ENUM[num], ifname)

        def on_props(interface_name: str, changed: dict, invalidated: list):
            if "Connectivity" in changed:
                val = changed["Connectivity"].value
                run_hooks(roots, STATE_ENUM[val], ifname)
        props.on_properties_changed(on_props)

        try:
            # hang here until task.cancel()
            await asyncio.get_event_loop().create_future()
        finally:
            # cleanup on cancellation
            props.off_properties_changed(on_props)
            logging.debug("detached IFACE=%s", ifname)
            def cleanup():
                props.off_properties_changed(on_props)

    # watch for new daemon instances
    def on_name_owner_changed(msg):
        if msg.member != "NameOwnerChanged":
            return
        name, old, new = msg.body
        m = re.match(rf'^{BUS_ROOT}\.([a-zA-Z0-9_]+)$', name)
        if m:
            ifname = m.group(1)
            if new:
                # a new connectivity-monitord@ifname appeared
                future = attach(ifname, f'{BASE_PATH}/{ifname}', name, True)
                active[ifname] = asyncio.create_task(future)
            else:
                task = active.pop(ifname, None)
                if task:
                    task.cancel()
                    run_hooks(roots, "unknown", ifname)
    bus.add_message_handler(on_name_owner_changed)

    # Keep track of active attachments
    active = {}

    # initial discovery: find all per-if services by name
    introspection = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    dbus_obj = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspection)
    dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")
    names = await dbus_iface.call_list_names()
    svc = [m.group(0, 1) for m in (re.match(rf'^{BUS_ROOT}\.([a-zA-Z0-9_]+)$', n) for n in names) if m]
    for name, ifname in svc:
        future = attach(ifname, f'{BASE_PATH}/{ifname}', name, args.run_startup_triggers)
        active[ifname] = asyncio.create_task(future)

    loop = asyncio.get_running_loop()
    n = sdnotify.SystemdNotifier()

    main_task = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    n.notify("READY=1")

    try:
        await main_task
    except asyncio.CancelledError:
        pass

def cli_entry() -> None:
    asyncio.run(main())

if __name__ == "__main__":
    cli_entry()
