# networkd-connectivity

A tiny **per-interface connectivity probe**

* written in pure Python 3 (aiohttp)
* one instance per NIC via a template systemd service
* metrics configured **inside the interface `.network` file**  
  (section `[ConnectivityMetric]`)
* automatic reload when `systemd-networkd` is reloaded

---

## Testing

To test connectivity failures, utilize qdisc:

    sudo tc qdisc add dev eno0 root netem loss 100% 100%

And of course remove the rule when done:

    sudo tc qdisc del dev eno0 root netem loss 100% 100%
