#!/usr/bin/env python3
"""
UDP sender (client) with sequence numbers, ACKs, retransmission and SDN loss reporting.

* Sends numbered DATA packets at --rate packets/s for --duration seconds.
* A packet is retransmitted when its ACK does not arrive within --rto seconds, or at once
  when the receiver sends a NACK for it (gap detected).  After --max-retries it is "failed".
* "First-attempt loss" = packets that needed a retransmission.  The sender keeps a sliding
  window (--window s) of first-attempt outcomes.  If the loss rate in that window exceeds
  --loss-threshold it sends a JSON loss_report to the SDN controller's UNIX datagram socket
  (rate-limited by --cooldown).  This is the application -> SDN integration point.
* A per-packet CSV (seq, first_send, ack_time, tries, status) is written to --log for the
  offline analysis (recovery time, loss before/after, latency).
"""
import argparse
import collections
import csv
import json
import select
import socket
import struct
import time

DATA, ACK, NACK, FIN, FINACK = 1, 2, 3, 4, 5
HDR = struct.Struct("!BId")


def send_report(path, payload):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(payload).encode(), path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dst", default="10.0.0.2")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--rate", type=float, default=50, help="packets per second")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--payload", type=int, default=200, help="bytes per packet")
    ap.add_argument("--rto", type=float, default=0.15, help="retransmission timeout (s)")
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--name", default="client")
    ap.add_argument("--log", default="/tmp/sender_log.csv")
    ap.add_argument("--report-loss", action="store_true", help="send loss reports to the SDN controller")
    ap.add_argument("--ctrl-sock", default="/tmp/sdn_ctrl.sock")
    ap.add_argument("--loss-threshold", type=float, default=0.10)
    ap.add_argument("--window", type=float, default=1.0)
    ap.add_argument("--min-events", type=int, default=20)
    ap.add_argument("--cooldown", type=float, default=3.0)
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    dst = (a.dst, a.port)
    pad = b"x" * max(0, a.payload - HDR.size)

    def tx(seq):
        sock.sendto(HDR.pack(DATA, seq, time.time()) + pad, dst)

    interval = 1.0 / a.rate
    t_start = time.time()
    t_end = t_start + a.duration
    next_send = t_start
    next_seq = 0
    pending = {}                       # seq -> [last_send, retransmissions, loss_counted]
    rec = {}                           # seq -> dict for the CSV
    window = collections.deque()       # (time, 1=first-attempt loss / 0=first-attempt ok)
    last_report, reports, retx_total = 0.0, 0, 0
    next_print = t_start + 1.0
    hard_stop = t_end + a.rto * (a.max_retries + 2)

    print("[%s] sending %.0f pkt/s for %.0fs to %s:%d (report_loss=%s)"
          % (a.name, a.rate, a.duration, a.dst, a.port, a.report_loss), flush=True)

    while True:
        now = time.time()

        # 1. new packets
        while next_send <= now and next_send < t_end:
            t = time.time()
            tx(next_seq)
            rec[next_seq] = dict(first_send=t, ack=None, tries=0, status="pending")
            pending[next_seq] = [t, 0, False]
            next_seq += 1
            next_send += interval

        # 2. timeouts -> retransmit or give up
        for seq, p in list(pending.items()):
            if now - p[0] >= a.rto:
                if not p[2]:
                    p[2] = True
                    window.append((now, 1))
                if p[1] >= a.max_retries:
                    rec[seq]["status"] = "failed"
                    del pending[seq]
                else:
                    p[1] += 1
                    p[0] = now
                    rec[seq]["tries"] = p[1]
                    retx_total += 1
                    tx(seq)

        # 3. receive ACK / NACK
        wait = max(0.0, min(next_send - time.time(), 0.005)) if now < t_end else 0.005
        if select.select([sock], [], [], wait)[0]:
            while True:
                try:
                    pkt, _ = sock.recvfrom(2048)
                except BlockingIOError:
                    break
                if len(pkt) < HDR.size:
                    continue
                typ, seq, _ts = HDR.unpack_from(pkt)
                t = time.time()
                if typ == ACK and seq in pending:
                    p = pending.pop(seq)
                    rec[seq]["ack"] = t
                    rec[seq]["status"] = "ok"
                    if not p[2]:
                        window.append((t, 0))
                elif typ == NACK and seq in pending:
                    p = pending[seq]
                    if t - p[0] > 0.02 and p[1] < a.max_retries:   # ignore NACKs for brand-new packets
                        if not p[2]:
                            p[2] = True
                            window.append((t, 1))
                        p[1] += 1
                        p[0] = t
                        rec[seq]["tries"] = p[1]
                        retx_total += 1
                        tx(seq)

        # 4. sliding-window loss detection -> tell the SDN controller
        now = time.time()
        while window and window[0][0] < now - a.window:
            window.popleft()
        if len(window) >= a.min_events:
            rate = sum(w[1] for w in window) / float(len(window))
            if a.report_loss and rate >= a.loss_threshold and now - last_report >= a.cooldown:
                ok = send_report(a.ctrl_sock, dict(event="loss_report", src=a.name,
                                                   loss=round(rate, 3), ts=now))
                last_report = now
                reports += 1
                print("[%s] t=%.1fs LOSS %.0f%% in last %.1fs -> report to controller (%s)"
                      % (a.name, now - t_start, rate * 100, a.window, "sent" if ok else "no controller socket"),
                      flush=True)

        if now >= next_print:
            acked = sum(1 for r in rec.values() if r["status"] == "ok")
            wl = (sum(w[1] for w in window) / float(len(window))) if window else 0.0
            print("[%s] t=%2.0fs sent=%d acked=%d retx=%d window_loss=%.0f%%"
                  % (a.name, now - t_start, next_seq, acked, retx_total, wl * 100), flush=True)
            next_print += 1.0

        if now >= t_end and not pending:
            break
        if now >= hard_stop:
            for seq in pending:
                rec[seq]["status"] = "failed"
            break

    # 5. close the session so the receiver writes its summary
    for _ in range(5):
        sock.sendto(HDR.pack(FIN, next_seq, time.time()), dst)
        if select.select([sock], [], [], 0.3)[0]:
            try:
                pkt, _ = sock.recvfrom(2048)
                if HDR.unpack_from(pkt)[0] == FINACK:
                    break
            except (BlockingIOError, struct.error):
                pass

    with open(a.log, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "first_send", "ack_time", "tries", "status"])
        for seq in sorted(rec):
            r = rec[seq]
            w.writerow([seq, "%.6f" % r["first_send"], "" if r["ack"] is None else "%.6f" % r["ack"],
                        r["tries"], r["status"]])

    ok = sum(1 for r in rec.values() if r["status"] == "ok")
    first_try = sum(1 for r in rec.values() if r["status"] == "ok" and r["tries"] == 0)
    print("[%s] DONE sent=%d delivered=%d failed=%d first_attempt_loss=%.1f%% retransmissions=%d loss_reports=%d"
          % (a.name, len(rec), ok, len(rec) - ok, 100.0 * (1 - first_try / float(max(1, len(rec)))),
             retx_total, reports), flush=True)


if __name__ == "__main__":
    main()
