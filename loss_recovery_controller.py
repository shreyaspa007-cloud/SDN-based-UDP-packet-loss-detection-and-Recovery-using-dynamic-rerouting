"""
Ryu (OpenFlow 1.3) controller for Project 7 - Packet Loss Detection and Recovery.

What it does
------------
1. PROACTIVE ROUTING  - on switch connect it installs IPv4 flows for the PRIMARY path
                        (s1-s2-s4) and pre-installs the ALTERNATE path (s1-s3-s4) on s3.
                        Only the ingress switches (s1, s4) are re-pointed when rerouting.
2. PATH MONITORING    - every POLL_INTERVAL s it reads port counters and computes the real
                        loss on each path link:  (tx on upstream port - rx on downstream port)
                        / tx.  Two consecutive polls above STATS_LOSS_THRESHOLD => reroute.
3. APP-DRIVEN CONTROL - the UDP application sends JSON "loss_report" datagrams to a UNIX
                        datagram socket (CTRL_SOCK).  Mininet hosts live in separate *network*
                        namespaces but share the filesystem, so a UNIX socket is the simplest
                        reliable host->controller channel.  A report triggers an immediate reroute.
4. LINK FAILURE       - OFPPortStatus (link down) on a primary-path port => immediate reroute.
5. MODES (set at runtime with {"event":"config","mode":...}):
       static      - never reroutes (baseline)
       stats_only  - reroutes only on port-stats / link-down (ignores application reports)
       dynamic     - stats + application reports + link-down  (full proposed system)
   Changing the mode also resets the network back to the primary path.

Every decision is appended (with a wall-clock timestamp) to EVENT_LOG so that the experiment
runner can compute detection / recovery times.
"""
import json
import os
import socket
import time

try:                                   # classic Ryu (Python <= 3.10)
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                        MAIN_DISPATCHER, set_ev_cls)
    from ryu.lib import hub
    from ryu.ofproto import ofproto_v1_3
except ImportError:                    # os-ken: maintained fork of Ryu, same API (Python >= 3.11)
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                           MAIN_DISPATCHER, set_ev_cls)
    from os_ken.lib import hub
    from os_ken.ofproto import ofproto_v1_3

_AppBase = getattr(app_manager, "OSKenApp", None) or app_manager.RyuApp

CTRL_SOCK = "/tmp/sdn_ctrl.sock"
EVENT_LOG = "/tmp/sdn_events.jsonl"

POLL_INTERVAL = 1.0          # seconds between port-stat requests
MIN_PKTS = 10                # ignore a poll interval with fewer packets than this
STATS_LOSS_THRESHOLD = 0.05  # 5 % link loss counts as "bad"
STATS_CONSEC = 2             # consecutive bad polls needed

H1, H2, H3 = "10.0.0.1", "10.0.0.2", "10.0.0.3"

# dpid -> {destination ip -> output port}.  Port numbers match topology.py.
PATHS = {
    "primary":   {1: {H1: 1, H3: 4, H2: 2},
                  2: {H1: 1, H3: 1, H2: 2},
                  4: {H1: 2, H3: 2, H2: 1}},
    "alternate": {1: {H1: 1, H3: 4, H2: 3},
                  3: {H1: 1, H3: 1, H2: 2},
                  4: {H1: 3, H3: 3, H2: 1}},
}
INGRESS = (1, 4)             # switches whose forwarding depends on the active path

# (tx counter: dpid, port) -> (rx counter: dpid, port) for each monitored link
LINKS = {"primary": ((2, 2), (4, 2)),
         "alternate": ((3, 2), (4, 3))}
PRIMARY_PORTS = {(1, 2), (2, 1), (2, 2), (4, 2)}   # a port-down here kills the primary path


class LossRecoveryController(_AppBase):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(LossRecoveryController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.mode = "dynamic"
        self.current = "primary"
        self.cycle = 0
        self.port_stats = {}      # (dpid, port) -> (cycle, rx_packets, tx_packets)
        self.prev = {}            # link name -> (tx, rx) at previous poll
        self.link_loss = {}
        self.bad_polls = 0
        open(EVENT_LOG, "w").close()
        hub.spawn(self._monitor_loop)
        hub.spawn(self._ctrl_server)
        self._log("controller_started", mode=self.mode)

    # ------------------------------------------------------------------ utilities
    def _log(self, event, **kw):
        rec = dict(ts=time.time(), event=event, **kw)
        self.logger.info("EVENT %s", json.dumps(rec))
        try:
            with open(EVENT_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def _add_flow(self, dp, dst_ip, out_port, priority=10):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
        actions = [parser.OFPActionOutput(out_port)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        # OFPFC_ADD with an identical match+priority overwrites the old entry
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst))

    def _install_path(self, dp, name):
        for dst_ip, port in PATHS[name].get(dp.id, {}).items():
            self._add_flow(dp, dst_ip, port)

    def _install_initial(self, dp):
        if dp.id in INGRESS:
            self._install_path(dp, self.current)
        elif dp.id == 2:
            self._install_path(dp, "primary")
        elif dp.id == 3:
            self._install_path(dp, "alternate")

    def _set_path(self, name):
        self.current = name
        for dpid in INGRESS:
            dp = self.datapaths.get(dpid)
            if dp:
                self._install_path(dp, name)

    def _reroute(self, reason, **info):
        if self.current != "primary":
            return
        self._set_path("alternate")
        self._log("reroute", reason=reason, to="alternate", **info)

    # ------------------------------------------------------------- OpenFlow events
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self._install_initial(dp)
        self._log("switch_connected", dpid=dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif self.datapaths.get(dp.id) is dp:
            del self.datapaths[dp.id]

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp, ofp = msg.datapath, msg.datapath.ofproto
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN)
        self._log("port_status", dpid=dp.id, port=msg.desc.port_no, down=down)
        if down and (dp.id, msg.desc.port_no) in PRIMARY_PORTS and self.mode != "static":
            self._reroute("port_down", dpid=dp.id, port=msg.desc.port_no)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            self.port_stats[(dpid, st.port_no)] = (self.cycle, st.rx_packets, st.tx_packets)

    # ------------------------------------------------------------- path monitoring
    def _monitor_loop(self):
        while True:
            hub.sleep(POLL_INTERVAL)
            self.cycle += 1
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(0.3)                       # let the replies arrive
            self._evaluate()

    def _evaluate(self):
        for name, (tx_key, rx_key) in LINKS.items():
            tx_s, rx_s = self.port_stats.get(tx_key), self.port_stats.get(rx_key)
            if not tx_s or not rx_s or tx_s[0] != self.cycle or rx_s[0] != self.cycle:
                continue
            tx, rx = tx_s[2], rx_s[1]
            prev, self.prev[name] = self.prev.get(name), (tx, rx)
            if prev is None:
                continue
            d_tx, d_rx = tx - prev[0], rx - prev[1]
            if d_tx < MIN_PKTS:
                self.link_loss[name] = None
                continue
            loss = max(0.0, (d_tx - d_rx) / float(d_tx))
            self.link_loss[name] = loss
            self._log("link_loss", link=name, loss=round(loss, 3), tx=d_tx, rx=d_rx)

            if name == "primary" and self.mode != "static" and self.current == "primary":
                self.bad_polls = self.bad_polls + 1 if loss >= STATS_LOSS_THRESHOLD else 0
                if self.bad_polls >= STATS_CONSEC:
                    alt = self.link_loss.get("alternate")
                    if alt is None or alt < STATS_LOSS_THRESHOLD:
                        self._reroute("link_stats", loss=round(loss, 3))

    # ------------------------------------------- application -> controller channel
    def _ctrl_server(self):
        if os.path.exists(CTRL_SOCK):
            os.unlink(CTRL_SOCK)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(CTRL_SOCK)
        os.chmod(CTRL_SOCK, 0o777)
        self.logger.info("control socket listening on %s", CTRL_SOCK)
        while True:
            data = sock.recv(4096)
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            self._handle_ctrl(msg)

    def _handle_ctrl(self, msg):
        ev = msg.get("event")
        if ev == "config":                       # used by the experiment runner
            self.mode = msg.get("mode", "dynamic")
            self.bad_polls = 0
            self.prev.clear()
            self.link_loss.clear()
            self._set_path("primary")
            self._log("config", mode=self.mode)
        elif ev == "loss_report":                # sent by udp_sender.py
            self._log("loss_report", src=msg.get("src"), loss=msg.get("loss"),
                      acted=(self.mode == "dynamic" and self.current == "primary"))
            if self.mode == "dynamic":
                self._reroute("app_loss_report", loss=msg.get("loss"), src=msg.get("src"))
