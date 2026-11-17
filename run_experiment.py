#!/usr/bin/env python3
"""
Automated experiment runner.   Usage (must be root):

    sudo ./venv/bin/python run_experiment.py                       # full comparison, ~6 min
    sudo ./venv/bin/python run_experiment.py --loss 30 --modes static dynamic --duration 20
    sudo ./venv/bin/python run_experiment.py --fault linkdown      # link-failure scenario
    sudo ./venv/bin/python run_experiment.py --two-clients         # h1 and h3 send together
    sudo ./venv/bin/python run_experiment.py --no-controller       # you started ryu-manager yourself

For every (fault level x mode) it:  resets the controller into <mode>, starts the receiver on h2,
starts the sender(s) on h1 (and h3), injects the fault on the s2-s4 link after --fault-at seconds,
waits for the transfer to finish and stores per-packet CSVs + controller events.
Finally it calls analyze.py to print the comparison table and write plots.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from mininet.clean import cleanup
from mininet.log import info, setLogLevel

import analyze
from topology import ALT_DELAY_MS, PRIMARY_DELAY_MS, build, set_link

ROOT = os.path.dirname(os.path.abspath(__file__))
CTRL_SOCK = "/tmp/sdn_ctrl.sock"
EVENT_LOG = "/tmp/sdn_events.jsonl"
SENDER = os.path.join(ROOT, "app", "udp_sender.py")
RECEIVER = os.path.join(ROOT, "app", "udp_receiver.py")
CONTROLLER_APP = os.path.join(ROOT, "controller", "loss_recovery_controller.py")


def ctrl_send(msg):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), CTRL_SOCK)
    finally:
        s.close()


def start_controller(out_dir, ryu_cmd=None):
    import importlib.util
    bindir = os.path.dirname(sys.executable)
    cands = [ryu_cmd] if ryu_cmd else [os.path.join(bindir, "ryu-manager"), os.path.join(bindir, "osken-manager"),
                                       shutil.which("ryu-manager"), shutil.which("osken-manager")]
    exe = next((c for c in cands if c and os.path.exists(c)), None)
    if exe:
        cmd = [exe, CONTROLLER_APP]
    else:
        mod = "ryu" if importlib.util.find_spec("ryu") else "os_ken"
        cmd = [sys.executable, "-c", "from %s.cmd.manager import main; main()" % mod, CONTROLLER_APP]
    log = open(os.path.join(out_dir, "controller.log"), "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):                                    # wait up to 30 s for OpenFlow port + ctrl socket
        if proc.poll() is not None:
            sys.exit("ryu-manager exited early - see %s/controller.log" % out_dir)
        try:
            socket.create_connection(("127.0.0.1", 6653), timeout=0.5).close()
            if os.path.exists(CTRL_SOCK):
                return proc
        except OSError:
            pass
        time.sleep(0.5)
    proc.terminate()
    sys.exit("controller did not come up - see %s/controller.log" % out_dir)


def read_events():
    ev = []
    try:
        for line in open(EVENT_LOG):
            try:
                ev.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return ev


def run_one(net, links, mode, fault, level, args, run_dir):
    os.makedirs(run_dir, exist_ok=True)
    set_link(links["primary"], PRIMARY_DELAY_MS, 0)
    set_link(links["alternate"], ALT_DELAY_MS, 0)
    net.configLinkStatus("s2", "s4", "up")
    ctrl_send({"event": "config", "mode": mode})           # also resets to the primary path
    time.sleep(1.5)
    open(EVENT_LOG, "w").close()

    h1, h2, h3 = net.get("h1", "h2", "h3")
    clients = [h1, h3] if args.two_clients else [h1]
    summary_path = os.path.join(run_dir, "receiver.jsonl")
    rlog = open(os.path.join(run_dir, "receiver.log"), "w")
    recv = h2.popen([sys.executable, RECEIVER, "--summary", summary_path], stdout=rlog, stderr=subprocess.STDOUT)
    time.sleep(0.7)

    procs = []
    for h in clients:
        slog = open(os.path.join(run_dir, "%s.log" % h.name), "w")
        cmd = [sys.executable, SENDER, "--dst", h2.IP(), "--rate", str(args.rate),
               "--duration", str(args.duration), "--name", h.name, "--report-loss",
               "--log", os.path.join(run_dir, "%s.csv" % h.name)]
        procs.append(h.popen(cmd, stdout=slog, stderr=subprocess.STDOUT))

    time.sleep(args.fault_at)
    fault_ts = time.time()
    if fault == "loss":
        set_link(links["primary"], PRIMARY_DELAY_MS, level)
    else:
        net.configLinkStatus("s2", "s4", "down")
    info("    fault '%s %s' injected at t=%ds\n" % (fault, level, args.fault_at))

    for p in procs:
        p.wait()
    time.sleep(0.5)
    recv.terminate()
    # restore the link for the next run
    set_link(links["primary"], PRIMARY_DELAY_MS, 0)
    net.configLinkStatus("s2", "s4", "up")

    events = read_events()
    shutil.copy(EVENT_LOG, os.path.join(run_dir, "events.jsonl"))
    reroute = next((e for e in events if e["event"] == "reroute" and e["ts"] >= fault_ts - 0.5), None)
    meta = dict(mode=mode, fault=fault, level=level, fault_ts=fault_ts, duration=args.duration,
                fault_at=args.fault_at, rate=args.rate, clients=[h.name for h in clients],
                reroute_ts=reroute["ts"] if reroute else None,
                reroute_reason=reroute["reason"] if reroute else None)
    json.dump(meta, open(os.path.join(run_dir, "meta.json"), "w"), indent=2)
    if reroute:
        info("    controller rerouted %.2fs after the fault (reason: %s)\n"
             % (reroute["ts"] - fault_ts, reroute["reason"]))
    else:
        info("    controller did NOT reroute\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modes", nargs="+", default=["static", "stats_only", "dynamic"],
                    choices=["static", "stats_only", "dynamic"])
    ap.add_argument("--fault", choices=["loss", "linkdown"], default="loss")
    ap.add_argument("--loss", nargs="+", type=int, default=[10, 20, 30],
                    help="per-direction loss %% injected on the s2-s4 link (use 0 for a no-fault control run)")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--fault-at", type=int, default=10)
    ap.add_argument("--rate", type=int, default=50)
    ap.add_argument("--two-clients", action="store_true", help="h1 and h3 both send to h2")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-controller", action="store_true", help="do not start ryu-manager (you run it)")
    ap.add_argument("--ryu-cmd", default=None, help="path to ryu-manager")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root:  sudo ./venv/bin/python run_experiment.py")
    setLogLevel("info")
    out = args.out or os.path.join(ROOT, "results", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    cleanup()

    ctrl = None if args.no_controller else start_controller(out, args.ryu_cmd)
    net = None
    try:
        net, links = build()
        time.sleep(2)
        info("*** Connectivity check (pingall)\n")
        if net.pingAll() > 0:
            sys.exit("pingall failed - check the controller log / flows")
        conds = [("loss", l) for l in args.loss] if args.fault == "loss" else [("linkdown", 100)]
        for fault, level in conds:
            for mode in args.modes:
                info("*** RUN  fault=%s level=%s mode=%s\n" % (fault, level, mode))
                run_one(net, links, mode, fault, level, args, os.path.join(out, "%s%s_%s" % (fault, level, mode)))
    finally:
        if net:
            net.stop()
        if ctrl:
            ctrl.terminate()
    analyze.analyze(out)


if __name__ == "__main__":
    main()
