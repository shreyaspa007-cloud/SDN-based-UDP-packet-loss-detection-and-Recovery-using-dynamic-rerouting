# SDN-based-UDP-packet-loss-detection-and-Recovery-using-dynamic-rerouting
The 2 widely used transport layer protocols,TCP and UDP have their own merits and demerits,but an important feature of UDP is how it can send packets extremely fast,but has a serious disadvantage of not detecting packet losses. We intended to solve this problem using SDN controller and Dynamic  rerouting.

# Packet Loss Detection and Recovery — UDP + SDN (Mininet + Ryu)

Project 7 of the SDN–Socket Programming mini project.
A UDP application detects packet loss with **sequence numbers**, recovers with **ACK/NACK + retransmission**,
and cooperates with an **SDN controller** that monitors the network and **dynamically reroutes** traffic
from a lossy primary path to an alternate path. Recovery time and packet loss before/after recovery are measured
and compared against a static-routing baseline.

---------------------------------------------------------------------------------------------------

## 1. Platform and language

| Item | Choice | Why |
|---|---|---|
| OS | **Ubuntu 22.04 (or 20.04) VM** — VirtualBox/VMware | Matches the course Mininet manual; Ryu does **not** work well on Python 3.12 (Ubuntu 24.04) |
| Network emulator | **Mininet 2.3** + **Open vSwitch** | Required by the rubric |
| SDN controller | **Ryu** (OpenFlow 1.3, Python) | Python, simple port-stats API, easy to explain in viva |
| Application language | **Python 3, standard library `socket` only** | "Low-level sockets" requirement; no frameworks |
| Fault injection | Linux `tc netem` (loss/delay) and `ip link down` | Real kernel-level loss on a Mininet link |
| Analysis | Python + matplotlib | Tables and plots for the report |

## 2. Prerequisites (must be true before anything works)

1. **VM**: ≥ 2 vCPU, ≥ 4 GB RAM, ≥ 20 GB disk, Ubuntu 20.04/22.04, **sudo** rights, **internet** (NAT/Bridged).
2. **Python 3.8–3.10** (default on 20.04/22.04). Check: `python3 --version`.
3. **Mininet + Open vSwitch** running: `sudo systemctl status openvswitch-switch` → *active*.
4. **netem kernel module** available: `sudo modprobe sch_netem` must succeed.
   (If not: `sudo apt install linux-modules-extra-$(uname -r)`.)
5. **Ryu 4.34 with `eventlet==0.30.2`** and `setuptools==58.2.0` (newer ones break Ryu).
6. Run Mininet **as root**; run the controller as the same user/root so both see `/tmp/sdn_ctrl.sock`.
7. Port **6653** free (OpenFlow) and UDP **5001** free inside the hosts.

## 3. Steps to do BEFORE coding / running (checklist)

```
[ ] 1. Create/boot the Ubuntu VM and make sure `ping google.com` works
[ ] 2. git clone <your-repo> && cd sdn-udp-loss-recovery
[ ] 3. ./setup.sh                      # installs mininet, OVS, ryu (venv), matplotlib, checks netem
[ ] 4. sudo mn --test pingall          # Mininet sanity check (0% dropped)
[ ] 5. sudo mn -c                      # clean old state
[ ] 6. python3 tests/loopback_test.py  # protocol sanity check, no root needed
[ ] 7. Read section 5 (architecture) — you must be able to explain it in the viva
```

## 4. Architecture

```
                 +----------- s2 -----------+   PRIMARY   path  s1-s2-s4  (5 ms)
   h1 (client) --+                          +-- s4 -- h2 (UDP server)
   h3 (client) --+- s1                       |
                 +----------- s3 -----------+   ALTERNATE path  s1-s3-s4  (10 ms)

   Ryu controller (OpenFlow 1.3, 127.0.0.1:6653) controls s1..s4
        ^  port-stats polling every 1 s (loss = tx(s2:2) - rx(s4:2))
        |  UNIX datagram socket /tmp/sdn_ctrl.sock  <--  JSON loss_report from the UDP client
        +  OFPPortStatus (link down)
```

**UDP protocol** (`type 1B | seq 4B | timestamp 8B | payload`): `DATA`, `ACK`, `NACK`, `FIN`, `FINACK`.

* Client numbers every DATA packet, retransmits on RTO (150 ms) or NACK, gives up after 6 retries.
* Server ACKs every packet, detects **gaps** in sequence numbers → sends NACK, counts duplicates, writes a per-client summary (supports several clients at once).
* Client keeps a 1-s sliding window of first-attempt outcomes; if loss ≥ 10 % it sends a `loss_report` to the controller.

**Controller modes** (set at runtime by the runner):

| Mode | Behaviour | Role |
|---|---|---|
| `static` | never reroutes | **baseline** |
| `stats_only` | reroutes on port-stats loss (≥5 % for 2 polls) or link-down | network-only monitoring |
| `dynamic` | stats **+ application loss reports** + link-down | proposed system |

Reroute = the controller re-writes the flows on the ingress switches **s1** and **s4** (both directions) so traffic goes via s3; s2/s3 flows are pre-installed.

> Why a UNIX socket for app→controller? Mininet hosts have separate *network* namespaces but share the *filesystem*, so a UNIX datagram socket is a dependable channel to a controller running in the root namespace. (Alternative: attach the controller to the topology with `connectToRootNS`.)

## 5. Project layout

```
topology.py                         Mininet diamond topology, set_link() helper (netem)
controller/loss_recovery_controller.py   Ryu app: flows, monitoring, reroute, control socket
app/udp_sender.py                   UDP client: seq numbers, ACK/NACK, retransmission, loss report
app/udp_receiver.py                 UDP server: gap detection, ACK/NACK, per-client summary
run_experiment.py                   Automated experiments (all modes x fault levels)
analyze.py                          Metrics, summary.md/csv, plots
tests/loopback_test.py              Mininet-free test with a lossy UDP proxy
setup.sh, requirements.txt          Environment setup
```

## 6. Running

### 6.1 Fully automated (recommended for the demo)

```bash
sudo ./venv/bin/python run_experiment.py                         # 3 modes x 3 loss levels (~6 min)
sudo ./venv/bin/python run_experiment.py --loss 30 --modes static dynamic --duration 20 --fault-at 8   # quick demo
sudo ./venv/bin/python run_experiment.py --fault linkdown        # link-failure scenario
sudo ./venv/bin/python run_experiment.py --two-clients           # h1 and h3 send simultaneously
sudo ./venv/bin/python run_experiment.py --loss 0 --modes static # control run, no fault
```
Output goes to `results/<timestamp>/`: `summary.md`, `summary.csv`, `timeline_*.png`, `comparison.png`,
per-run `h1.csv`, `events.jsonl` (controller decisions), `controller.log`.

### 6.2 Manual (good for showing the live demo / viva)

```bash
# terminal 1 – controller
./venv/bin/ryu-manager controller/loss_recovery_controller.py

# terminal 2 – network
sudo ./venv/bin/python topology.py
mininet> pingall
mininet> h2 python3 app/udp_receiver.py &
mininet> h1 python3 app/udp_sender.py --dst 10.0.0.2 --duration 40 --report-loss --name h1 --log /tmp/h1.csv &
# ... after ~10 s inject 30 % loss in both directions of the primary link:
mininet> sh tc qdisc replace dev s2-eth2 root netem delay 5ms loss 30%
mininet> sh tc qdisc replace dev s4-eth2 root netem delay 5ms loss 30%
# watch the client print "LOSS ..% -> report to controller", then check the rerouted flows:
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s3        # packet counters now increase on s3
```
To use the `static` baseline manually, send `{"event":"config","mode":"static"}` to `/tmp/sdn_ctrl.sock`
(`python3 -c "import socket,json;s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.sendto(json.dumps({'event':'config','mode':'static'}).encode(),'/tmp/sdn_ctrl.sock')"`).

## 7. Test cases

| # | Test | How | Expected |
|---|---|---|---|
| T1 | Connectivity | `pingall` | 0 % dropped |
| T2 | Normal operation | `--loss 0 --modes dynamic` | 0 % first-attempt loss, no reroute |
| T3 | Loss detection & recovery by retransmission | `tests/loopback_test.py` | gaps detected, 0 packets missing at the end |
| T4 | Baseline under loss | `--modes static` | loss stays high after fault, never "recovers", higher latency |
| T5 | SDN reroute (stats only) | `--modes stats_only` | reroute ≈ 2–3 s after fault |
| T6 | SDN reroute (stats + app report) | `--modes dynamic` | reroute ≈ 0.3–1.5 s after fault, loss returns to 0 % |
| T7 | Link failure | `--fault linkdown` | `port_down` reroute in ≈ ms; static baseline loses everything |
| T8 | Multiple clients | `--two-clients` | both h1 and h3 recover; receiver shows two sessions |
| T9 | Multiple loss levels | `--loss 10 20 30` | detection works at each level |

## 8. Metrics (what `analyze.py` reports)

* **pre / post / steady loss %** — first-attempt loss before the fault, after it, and after recovery.
* **residual %** — packets still undelivered after all retransmissions.
* **latency mean / p95** — first send → ACK, for packets sent after the fault (retransmissions inflate it).
* **retx %** — retransmission overhead.
* **detect s** — fault → controller reroute.
* **recovery s** — fault → first 60 consecutive packets (1.2 s) delivered on the first attempt.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `ImportError: cannot import name 'ALREADY_HANDLED'` | `./venv/bin/pip install eventlet==0.30.2` |
| `tc: Specified qdisc not found` / RTNETLINK | `sudo apt install linux-modules-extra-$(uname -r)` |
| `pingall` shows loss | controller not connected: `cat results/*/controller.log`; `sudo mn -c` and retry |
| `Cannot find device "s2-eth2"` | stale state: `sudo mn -c` |
| Port 6653 in use | `sudo pkill -f ryu-manager` |
| `ryu-manager: command not found` under sudo | run with `sudo ./venv/bin/python …` (the runner finds `venv/bin/ryu-manager`) or `--ryu-cmd /full/path` |
| Controller not rerouting | check `results/*/events.jsonl` for `loss_report` / `link_loss` events |

## 10. Rubric mapping

| Rubric item | Where |
|---|---|
| Problem understanding & architecture | §4 |
| TCP/UDP sockets | `app/*.py` (raw `socket`, ACK/NACK/FIN protocol, multi-client, error handling) |
| Mininet + SDN implementation | `topology.py`, `controller/…` (proactive flows, alternate path, flow rewrite) |
| Socket–SDN integration | `loss_report` UNIX socket → reroute; compare `stats_only` vs `dynamic` |
| Dynamic SDN functionality | monitoring + rerouting + recovery (3 triggers) |
| Challenging scenario | random loss at 3 levels, link failure, two concurrent clients |
| Performance evaluation | `run_experiment.py` + `analyze.py` (static baseline vs SDN) |
| Documentation / reproducibility | this README, `setup.sh`, fixed commands |

## 11. Submitting to GitHub

```bash
git init && git add . && git commit -m "SDN UDP packet loss detection and recovery"
git branch -M main && git remote add origin https://github.com/<you>/<repo>.git && git push -u origin main
```
Add your `results/<run>/summary.md` and the PNG plots, and screenshots of the demo, to the repo / report.

## 12. Likely viva questions

* Why UDP + own ACK/retransmission instead of TCP? (control over detection and timing, the project asks for UDP)
* How is loss detected? (receiver: sequence gaps → NACK; sender: RTO + sliding-window loss rate)
* How does the controller know about loss? (port-counter difference across the link **and** application reports)
* Why modify only s1 and s4 on reroute? (s2/s3 flows pre-installed; ingress rewrite is atomic per direction)
* Why does `dynamic` react faster than `stats_only`? (poll interval ×2 consecutive polls vs a ~1 s application window)
* What happens in `static` mode? (no reroute → app-level retransmission only → higher latency/overhead)
