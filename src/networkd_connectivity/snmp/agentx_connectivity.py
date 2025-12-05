#!/usr/bin/env python3
# AgentX sub-agent that mirrors each connectivity-monitord instance
# into an SNMP table row indexed by ifIndex.
#
# Run (unprivileged) with write access to the master agentX socket:
#   AGENTX_SOCKET=/var/agentx/master ./agentx_connectivity.py

import sdnotify
import pathlib
import pyagentx
import subprocess
import logging

OID_ROOT = "1.3.6.1.4.1.99999.1.1"

def ifindex_for(name: str) -> int:
    return int(pathlib.Path(f"/sys/class/net/{name}/ifindex").read_text())

class ConnTable(pyagentx.Updater):
    def update(self):
        procs = subprocess.run(['connectivity-state', '--no-header'],
                               check=False, stdout=subprocess.PIPE, text=True)
        for line in procs.stdout.splitlines():
            iface, state, _ = line.split()
            ifidx = ifindex_for(iface)
            names = ["unknown", "none", "portal", "limited", "full"]
            s_str = names[int(state)] if 0 <= int(state) < len(names) else "unknown"

            self.set_INTEGER(f"1.{ifidx}", ifidx)
            self.set_INTEGER(f"2.{ifidx}", int(state))
            self.set_OCTETSTRING(f"3.{ifidx}", s_str)

class ConnAgent(pyagentx.Agent):
    def setup(self):
        self.register(OID_ROOT, ConnTable)

def main():
    pyagentx.setup_logging()
    logger = logging.getLogger('pyagentx')
    logger.setLevel(logging.ERROR)
    n = sdnotify.SystemdNotifier()
    agent = ConnAgent()
    n.notify("READY=1")
    agent.start()

if __name__ == "__main__":
    main()
