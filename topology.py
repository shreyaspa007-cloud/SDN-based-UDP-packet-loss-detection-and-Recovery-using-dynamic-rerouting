#!/usr/bin/env python3
"""
Diamond topology for Project 7 - Packet Loss Detection and Recovery.

                 +----- s2 -----+          primary   path: s1 - s2 - s4  (5 ms on s2-s4)
   h1 --- s1 ----+              +---- s4 --- h2
   h3 ---/       +----- s3 -----+          alternate path: s1 - s3 - s4  (10 ms on s3-s4)

Explicit OpenFlow port numbers (the controller relies on them):
    s1: 1->h1  2->s2  3->s3  4->h3
    s2: 1->s1  2->s4
    s3: 1->s1  2->s4
    s4: 1->h2  2->s2  3->s3

h1 and h3 are UDP clients (senders), h2 is the UDP server (receiver).

Run standalone for a manual session:   sudo python3 topology.py
"""
from mininet.cli import CLI
from mininet.link import Link
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

PRIMARY_DELAY_MS = 5
ALT_DELAY_MS = 10


def set_link(link, delay_ms, loss_pct):
    """(Re)configure delay + random loss on BOTH directions of a link using netem."""
    for intf in (link.intf1, link.intf2):
        out = intf.node.cmd("tc qdisc replace dev %s root netem delay %sms loss %s%%"
                            % (intf.name, delay_ms, loss_pct))
        if "RTNETLINK" in out or "Error" in out:
            raise RuntimeError("tc/netem failed on %s: %s\n"
                               "Try: sudo apt install linux-modules-extra-$(uname -r)"
                               % (intf.name, out.strip()))


def build(ip="127.0.0.1", port=6653):
    """Create, start and return (net, links). links = {'primary': Link, 'alternate': Link}."""
    net = Mininet(controller=None, switch=OVSSwitch, link=Link, build=False)
    net.addController("c0", controller=RemoteController, ip=ip, port=port)

    h1 = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3/24", mac="00:00:00:00:00:03")
    s1, s2, s3, s4 = [net.addSwitch(n, protocols="OpenFlow13") for n in ("s1", "s2", "s3", "s4")]

    net.addLink(h1, s1, port2=1)
    net.addLink(s1, s2, port1=2, port2=1)
    net.addLink(s1, s3, port1=3, port2=1)
    net.addLink(h3, s1, port2=4)
    primary = net.addLink(s2, s4, port1=2, port2=2)
    alternate = net.addLink(s3, s4, port1=2, port2=3)
    net.addLink(h2, s4, port2=1)

    net.start()
    net.staticArp()                       # no ARP flooding needed (the topology has a loop)
    if hasattr(net, "waitConnected"):
        net.waitConnected()
    else:
        import time
        time.sleep(3)

    links = {"primary": primary, "alternate": alternate}
    set_link(primary, PRIMARY_DELAY_MS, 0)
    set_link(alternate, ALT_DELAY_MS, 0)
    return net, links


if __name__ == "__main__":
    setLogLevel("info")
    net, _links = build()
    info("*** Topology up. Handy commands inside the CLI:\n"
         "    h2 python3 app/udp_receiver.py &\n"
         "    h1 python3 app/udp_sender.py --dst 10.0.0.2 --duration 30 --log /tmp/h1.csv\n"
         "    sh tc qdisc replace dev s2-eth2 root netem delay 5ms loss 30%\n"
         "    sh tc qdisc replace dev s4-eth2 root netem delay 5ms loss 30%\n"
         "    sh ovs-ofctl -O OpenFlow13 dump-flows s1\n")
    CLI(net)
    net.stop()
