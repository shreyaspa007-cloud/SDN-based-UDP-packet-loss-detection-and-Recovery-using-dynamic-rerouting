#!/usr/bin/env python3
"""
Mininet-free test of the UDP protocol (sequence numbers, ACK/NACK, retransmission).
A tiny proxy drops packets in BOTH directions with probability --loss, so no root is needed.

    python3 tests/loopback_test.py --loss 0.2
"""
import argparse
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def proxy(listen_port, server, loss, stop):
    down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    down.bind(("127.0.0.1", listen_port))
    down.settimeout(0.2)
    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.settimeout(0.2)
    client = [None]

    def back():
        while not stop.is_set():
            try:
                pkt, _ = up.recvfrom(4096)
            except socket.timeout:
                continue
            if client[0] and random.random() >= loss:
                down.sendto(pkt, client[0])

    threading.Thread(target=back, daemon=True).start()
    while not stop.is_set():
        try:
            pkt, addr = down.recvfrom(4096)
        except socket.timeout:
            continue
        client[0] = addr
        if random.random() >= loss:
            up.sendto(pkt, server)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loss", type=float, default=0.2)
    ap.add_argument("--duration", type=int, default=6)
    a = ap.parse_args()
    random.seed(1)

    summ, log = "/tmp/loopback_summary.jsonl", "/tmp/loopback_sender.csv"
    for p in (summ, log):
        if os.path.exists(p):
            os.remove(p)
    stop = threading.Event()
    recv = subprocess.Popen([sys.executable, os.path.join(ROOT, "app", "udp_receiver.py"),
                             "--ip", "127.0.0.1", "--port", "5601", "--summary", summ],
                            stdout=subprocess.DEVNULL)
    threading.Thread(target=proxy, args=(5600, ("127.0.0.1", 5601), a.loss, stop), daemon=True).start()
    time.sleep(0.5)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "app", "udp_sender.py"),
                          "--dst", "127.0.0.1", "--port", "5600", "--duration", str(a.duration),
                          "--rate", "50", "--log", log, "--name", "test"],
                         capture_output=True, text=True).stdout
    time.sleep(0.5)
    stop.set()
    recv.terminate()
    print(out.strip().splitlines()[-1])
    rec = json.loads(open(summ).read().strip().splitlines()[-1])
    print("receiver summary:", rec)
    assert rec["gaps_detected"] > 0 or a.loss == 0, "receiver should have detected sequence gaps"
    assert rec["missing_final"] <= max(2, 0.02 * rec["expected"]), "too many packets unrecovered"
    print("PASS: loss detected via sequence numbers and recovered by retransmission")


if __name__ == "__main__":
    main()
