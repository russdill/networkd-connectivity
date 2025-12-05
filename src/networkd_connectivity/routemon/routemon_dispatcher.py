#!/usr/bin/env python3
# routemon_dispatcher.py - monitor default route changes and dispatch notification scripts

import ipaddress
import logging
import socket
import subprocess
import argparse
import pathlib
import os

from pyroute2 import IPRoute
from sdnotify import SystemdNotifier

def first_ipv4_address(ipr: IPRoute, ifindex: int) -> str:
    """Return first IPv4 address (without mask) assigned to interface."""
    addrs = ipr.get_addr(index=ifindex, family=socket.AF_INET)
    return str(ipaddress.ip_address(addrs[0].get_attr('IFA_ADDRESS'))) if addrs else None

def default_iface(ipr: IPRoute) -> tuple:
    """Return the current 'best' (lowest metric) IPv4 default route."""
    defaults = [r for r in ipr.get_routes(family=socket.AF_INET) if r['dst_len'] == 0]
    if not defaults:
        return (None, None)
    # choose the one with the lowest metric (RTA_PRIORITY)
    def _metric(r):
        return dict(r['attrs']).get('RTA_PRIORITY', 0)

    best = min(defaults, key=_metric)
    attrs = dict(best['attrs'])
    oif = best.get('oif') or attrs.get('RTA_OIF')
    return (ipr.get_links(oif)[0].get_attr('IFLA_IFNAME'), first_ipv4_address(ipr, oif), attrs.get('RTA_GATEWAY', None))

def monitor() -> None:
    """Monitor netlink for default-route changes; call ddclient when necessary."""
    ap = argparse.ArgumentParser(description="Default route hook dispatcher")
    ap.add_argument("-S", "--script-dir",
                    help="colon-separated list of hook paths",
                    default="/usr/lib/routemon-dispatcher.d:/etc/routemon-dispatcher.d")
    ap.add_argument("-T", "--run-startup-triggers", action="store_true",
                    help="invoke hooks once for the current state on start")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s")

    roots = [pathlib.Path(p) for p in args.script_dir.split(":")]
    logging.info("hook search path: %s", ":".join(str(r) for r in roots))

    n = SystemdNotifier()

    def update(ifname: str, addr: str, gateway: str) -> None:
        n.notify(f'STATUS={ifname}/{addr}->{gateway}')
        env = os.environ.copy()
        env["IFACE"] = ifname or ''
        env["IPV4_ADDR"] = addr or ''
        env["IPV4_GATEWAY"] = gateway or ''
        for root in roots:
            if not root.is_dir():
                continue
            for script in sorted(root.iterdir()):
                if not os.access(script, os.X_OK):
                    continue
                try:
                    subprocess.Popen([str(script)], env=env)
                except Exception as exc:
                    logging.error("failed to run %s: %s", script, exc)

    with IPRoute() as ipr:
        ipr.bind(async_msg=False)  # ensure blocking recv
        best = default_iface(ipr)
        logging.info("Service ready - default route on %s via %s from %s", best[0], best[2], best[1])
        update(*best)
        n.notify("READY=1")

        # subscribe to all route-related multicast groups and loop forever
        while True:
            for msg in ipr.get():  # drain current message queue
                if msg.get('event') not in ("RTM_NEWROUTE", "RTM_DELROUTE"):
                    continue
                if msg.get('family') != socket.AF_INET or msg.get('dst_len') != 0:
                    continue
                new_best = default_iface(ipr)
                if new_best == best:
                    continue
                best = new_best
                logging.info("New default route on %s via %s from %s", best[0], best[2], best[1])
                update(*best)

def main() -> None:
    try:
        monitor()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()

