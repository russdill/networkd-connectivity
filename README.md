# networkd-connectivity

**Multi-WAN Failover for systemd-networkd.**

This group of daemons and hooks provides connectivity monitoring and automated
connectivity failover (e.g., switching between Fiber and 5G/LTE). It also can
easily execute state change actions, such as updating dynamic DNS entries or
firing off notifications.

This serves a similar purpose to the NetworkManager connectivity monitor
architecture. Actively probing connections and updating the routing metrics
when state changes occur.

## Components & Architecture

The system achieves reliable failover via three primary daemons:

* `connectivity-monitord`: Runs per-interface and periodically performs
  connectivity probes. Publishes link state to dbus (`full`, `portal`,
  `limited`, `none`).

* `connectivity-dispatcher`: Listens for state changes from the monitoring
  daemon via dbus and executes appropriate hook scripts
  `/usr/lib/connectivity-dispatcher/<state>.d/`. A script is provided that
  updates the routing metric based on the connectivity state to facilitate
  automatic failover.

* `routemon-dispatcher`: Monitors the route table for changes to the lowest
  metric route and executes hook scripts. Intended to perform dynamic DNS
  updates (`ddclient`).

## Installation

### Debian/Ubuntu

This project is packaged for Debian. You can build it using:

```bash
dpkg-buildpackage -us -uc
```

The following packages are produced:

* `networkd-connectivity_<ver>_all.deb`: Connectivity monitor and dispatcher
  daemons.
* `networkd-connectivity-hooks_<ver>_all.deb`: Connectivity `metric_hook.py`
  for updating routing metric based on connectivity state.
* `routemon-dispatcher_<ver>_all.deb`: routemon daemon.
* `routemon-dispatcher-hooks_<ver>_all.deb`: routemon for updating dynamic DNS
  via `ddclient`.
* `networkd-connectivity-snmp_<ver>_all.deb`: AgentX subagent to export
  per-interface connectivity state to SNMP.

## Configuration

### Basic Connectivity Check

The `connectivity-monitord` contains sensible defaults, but can be customized
within the systemd `.network` file associated with the interface (e.g.,
`/etc/systemd/network/10-eth0.network`) by adding a `[ConnectivityMonitord]`
section. The timeout, interval between probes, and URLs probed can be
configured.

The systemd service must be manually enabled:

```bash
systemctl enable connectivity-monitord@eth0
systemctl start connectivity-monitord@eth0
```

The status can be observed with the `connectivity-state` cli.

The `ProbeURL=` key appends URLs to the list of URLs to be tested. In any given
interval, the connection state is determined by the URL with the best
connectivity. The default URLs are:

```ini
ProbeURL=http://nmcheck.gnome.org/check_network_status.txt=NetworkManager%20is%20online
ProbeURL=http://gstatic.com/generate_204
ProbeURL=http://example.com/=...Example%20Domain...
```

Additional `ProbeURL=` entries append to this list. Any empty `ProbeURL=` entry will clear the list.

* `http://site.com/` -- Expects empty body.
* `http://site.com/=TEXT` -- Expects body to be *exactly* `TEXT`.
* `http://site.com/=...TEXT...` -- Expects `TEXT` to appear anywhere in the body.
* `http://site.com/=TEXT...` -- Expects body to start with `TEXT`.

`Interval=` (default 15) gives the time period in seconds between sets of probes
and `Timeout=` (default 5) gives the amount of time to wait for each probe to
complete.

```ini
[Match]
Name=eth0

[Network]
DHCP=yes

# Configure connectivity-monitord
[ConnectivityMonitord]
# Clear default URLs
ProbeURL=

# Expect HTTP 204 (No Content)
ProbeURL=http://gstatic.com/generate_204

# Expect specifics string in response
ProbeURL=http://nmcheck.gnome.org/check_network_status.txt=NetworkManager is online

# Expect "Success" anywhere in the body
ProbeURL=http://captive.apple.com/hotspot-detect.html=...Success...

Interval=15
Timeout=5
```

### Route Metric Adjustment (Failover)

The `metric_hook.py` connectivity dispatcher hook is responsible for updating
routing metric based on interface connectivity state and can be configured in
the interface's associated `.network` file. Changes are only made if a
configuration entry exists for a given state.

```ini
[ConnectivityMetric]
# Desired metric when connectivity is FULL
full=100
# Desired metric when connectivity is LIMITED/PORTAL/NONE
portal=2000
limited=2000
none=2000

# Hysteresis configuration (prevent ping-ponging)
hysteresisdelay=180
hysteresisbackoff=600
```

 `HysteresisDelay=` (default 180 seconds) and `HysteresisBackoff=` (default 600
 seconds) are present to prevent excessive ping-ponging. If a connection is
 switching between `full` and some other state and back to `full` within a
 `HysteresisBackoff` period, it will be forced to wait `HysteresisDelay` before
 being considered to be back to `full`.

### Dynamic DNS (`ddclient`)

The provided `ddclient.sh` hook executes whenever the current best default route
changes. It requires ddclient to be configured and to automatically obtain the
current outgoing source IP address, eg:

> `use=cmd, cmd="ip route get 8.8.8.8 | sed -n 's/.* src \\([0-9.]\\+\\).*/\\1/p'"`

Note: ddclient >= 4.0.0 should use `usev4=cmdv4, cmdv4=...` instead.

## Usage

### Systemd Service

Enable the service for a specific interface:

```bash
sudo systemctl enable --now connectivity-monitord@eth0.service
```

This will start the probe for `eth0`, reading configuration from your network
settings.

### CLI Status Tool (`connectivity-state`)

The `connectivity-state` command allows you to query the current status of all managed interfaces via D-Bus.

```bash
# Show status of all interfaces
connectivity-state

# Output example:
# eth0 4 (full)
# wlan0 2 (portal)
```

### SNMP Helper (`networkd-connectivity-snmp`)

If the `networkd-connectivity-snmp` package is installed, an AgentX sub-agent service (`agentx-connectivity.service`) is available to export connectivity states to your system's `snmpd`.

**Requirements:**
1.  Verify `snmpd` is running: `sudo systemctl start snmpd`
2.  Enable AgentX support in `/etc/snmp/snmpd.conf`:
    ```conf
    master agentx
    ```
3.  Restart `snmpd`: `sudo systemctl restart snmpd`
4.  Start the sub-agent: `sudo systemctl enable --now agentx-connectivity`

**Querying with `snmpwalk`:**

The package includes a custom MIB `NETWORKD-CONNECTIVITY-MIB`. To load it automatically, add `mibs +NETWORKD-CONNECTIVITY-MIB` to your local `~/.snmp/snmp.conf`.

```bash
# Query the custom tree
snmpwalk -v2c -c public localhost NETWORKD-CONNECTIVITY-MIB::networkdConnectivityTable

# Example Output:
# NETWORKD-CONNECTIVITY-MIB::ifIndex.2 = INTEGER: 2
# NETWORKD-CONNECTIVITY-MIB::ifIndex.3 = INTEGER: 3
# NETWORKD-CONNECTIVITY-MIB::connectivityState.2 = INTEGER: 4
# NETWORKD-CONNECTIVITY-MIB::connectivityState.3 = INTEGER: 3
# NETWORKD-CONNECTIVITY-MIB::connectivityString.2 = STRING: "full"
# NETWORKD-CONNECTIVITY-MIB::connectivityString.3 = STRING: "limited"
```

## Dependencies

*   `Python 3.10+`
*   `aiohttp`
*   `dbus-next`
*   `pyroute2` (for routemon/hooks)
*   `sdnotify`
*   `pyagentx` (optional, for SNMP)

## Development

### Architecture

*   **D-Bus**: Each monitored interface is exposed on the system bus.
    *   **Service**: `io.github.russdill.networkd_connectivity.<ifname>`
    *   **Object**: `/io/github/russdill/networkd_connectivity/<ifname>`
    *   **Interface**: `io.github.russdill.networkd_connectivity1.Device`
    *   **Property**: `Connectivity` (uint32: 0=Unknown, 1=None, 2=Portal,
        3=Limited, 4=Full)

*   **Components**:
    *   `src/networkd_connectivity/daemon.py`: The core `connectivity-monitord`
        service.
    *   `src/networkd_connectivity/dispatcher.py`: Listens to D-Bus signals and
        executes helper scripts.
    *   `src/networkd_connectivity/routemon/routemon_dispatcher.py`: Watches
        netlink for route changes.
    *   `src/networkd_connectivity/_resources/`: Systemd units, sysusers.d
        config, and default hooks.

### Building

The project uses `hatchling` for building and `dh-python` for Debian packaging.

To build the Debian package locally:

```bash
# Install build dependencies
sudo apt install debhelper dh-python python3-all python3-setuptools \
    python3-hatchling pybuild-plugin-pyproject dh-exec python3-aiohttp \
    python3-dbus-next python3-sdnotify python3-pyroute2

# Build
dpkg-buildpackage -us -uc
```

### Testing (Simulation)

You can simulate network failures using `tc` (Traffic Control) to drop packets,
forcing the daemon to detect a "down" state without physically unplugging
cables.

```bash
# 1. Start monitoring log
journalctl -fu connectivity-monitord@eno0

# 2. Drop 100% of packets on eno0 (simulate cable cut)
sudo tc qdisc add dev eno0 root netem loss 100%

# ... Observe state change to 'limited' or 'none' ...

# 3. Restore connectivity
sudo tc qdisc del dev eno0 root netem
```
