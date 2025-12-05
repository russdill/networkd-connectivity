#!/usr/bin/env python3
import os
import subprocess
import configparser
import collections
import time
import sys
import logging
from pathlib import Path

SECTION      = "ConnectivityMetric"
UNIT_TMPL    = "connectivity-metric-delay@{iface}.service"
TIMESTAMP_DIR= Path("/run/connectivity-dispatcher")
DEFAULT_DELAY = 180
DEFAULT_BACKOFF = 600

class ConfigParserMultiValues(collections.OrderedDict):
    def __setitem__(self, key, value):
        if key in self and isinstance(value, list):
            self[key].extend(value)
        else:
            super().__setitem__(key, value)

    @staticmethod
    def getlist(value):
        return value.split(os.linesep)

def load_settings(iface: str, state: str) -> dict:
    # Parse [ConnectivityMetric] from networkctl cat @<iface>
    try:
        merged = subprocess.check_output(
            ["networkctl", "cat", "@" + iface], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return {}

    cp = configparser.ConfigParser(
            strict=False, empty_lines_in_values=False,
            dict_type=ConfigParserMultiValues,
            converters={"list": ConfigParserMultiValues.getlist})
    cp.read_string(merged)
    if not cp.has_section(SECTION):
        return {}

    section = cp[SECTION]

    return {
        "metric": section.getint(state, None),
        "delay": section.getfloat("hysteresisdelay", DEFAULT_DELAY),
        "backoff": section.getfloat("hysteresisbackoff", DEFAULT_BACKOFF),
    }

def run(cmd: list[str], **kwargs) -> None:
    try:
        subprocess.run(cmd, check=False, **kwargs)
    except:
        pass

def last_full_ts(iface: str) -> float | None:
    fn = TIMESTAMP_DIR / f"{iface}.last_full"
    try:
        return float(fn.read_text())
    except Exception:
        return None

def write_full_ts(iface: str) -> None:
    TIMESTAMP_DIR.mkdir(parents=True, exist_ok=True)
    (TIMESTAMP_DIR / f"{iface}.last_full").write_text(str(time.monotonic()))

def skip_hysteresis(iface: str, backoff: float) -> bool:
    ts = last_full_ts(iface)
    return ts is None or (time.monotonic() - ts) >= backoff

def main():
    if len(sys.argv) == 3:
        # Call from delayed systemd-run
        iface = sys.argv[1]
        state = sys.argv[2]
        nodelay = True
    else:
        # Call from dispatch
        iface = os.environ.get("IFACE")
        state = os.environ.get("STATE")
        if not iface or not state:
            sys.exit(1)
        nodelay = False

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # load per-iface metric
    settings = load_settings(iface, state)
    metric = settings.get("metric", None)
    if metric is None:
        logging.info("%s: No metric configured for %s", iface, state)
        sys.exit(0)

    delay = settings["delay"]
    backoff = settings["backoff"]

    # cancel any pending delayed-full job
    if not nodelay:
        unit = UNIT_TMPL.format(iface=iface)
        run(["systemctl", "stop", unit.replace('.service', '.*')])

    result = subprocess.run(['ip', 'route', 'show', 'to', 'default', 'dev', iface], stdout=subprocess.PIPE)
    current = result.stdout.decode('ascii').split()
    new = current[:]
    idx = current.index('metric')
    if idx != -1:
        if int(new[idx + 1]) == metric:
            sys.exit(0)
        new[idx + 1] = str(metric)
    else:
        new.extend(['metric', str(metric)])

    cmd_add = ['ip', 'route', 'add'] + new
    cmd_del = ['ip', 'route', 'del'] + current
    if nodelay or state != "full" or skip_hysteresis(iface, backoff):
        logging.info("%s/%s: Updating routing metric to %d", iface, state, metric)
        run(cmd_add)
        run(cmd_del)
    else:
        logging.info("%s/%s: Updating routing metric to %d with %ds delay", iface, state, metric, delay)
        run(["systemd-run", f"--unit={unit}", f"--on-active={delay}s", sys.argv[0], iface, state])
    if not nodelay and state == "full":
        write_full_ts(iface)

if __name__ == "__main__":
    main()
