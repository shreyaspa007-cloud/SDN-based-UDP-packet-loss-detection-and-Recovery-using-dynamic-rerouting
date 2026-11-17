#!/usr/bin/env python3
"""
UDP receiver (server) with sequence-number based loss detection.

Packet format (network byte order):  type(1B) | seq(4B) | timestamp(8B double) | payload...
    DATA=1  sender -> receiver     ACK=2   receiver -> sender (echoes seq + timestamp)
    NACK=3  receiver -> sender (seq = a missing sequence number)
    FIN=4   sender -> receiver (seq = total packets sent)     FINACK=5  reply to FIN

For every client (ip,port) the receiver keeps its own session, so several hosts can send
simultaneously.  A gap in the sequence numbers is reported to the sender with a NACK, every
DATA packet is ACKed, and a per-client summary is appended to --summary when the FIN arrives.
"""
import argparse
import json
import socket
import struct
import time

DATA, ACK, NACK, FIN, FINACK = 1, 2, 3, 4, 5
HDR = struct.Struct("!BId")
MAX_NACKS_PER_GAP = 16


class Session(object):
    def __init__(self):
        self.received = set()
        self.highest = -1
        self.dups = 0
        self.gaps = 0
        self.nacked = set()
        self.start = time.time()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--summary", default="/tmp/receiver_summary.jsonl")
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((a.ip, a.port))
    sock.settimeout(0.5)
    sessions = {}
    print("[receiver] listening on %s:%d" % (a.ip, a.port), flush=True)

    try:
        while True:
            try:
                pkt, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if len(pkt) < HDR.size:
                continue
            typ, seq, ts = HDR.unpack_from(pkt)

            if typ == DATA:
                s = sessions.setdefault(addr, Session())
                if seq in s.received:
                    s.dups += 1
                else:
                    if seq > s.highest + 1:                       # gap => packets missing
                        missing = [m for m in range(s.highest + 1, seq) if m not in s.received]
                        s.gaps += len(missing)
                        for m in missing[:MAX_NACKS_PER_GAP]:
                            if m not in s.nacked:
                                s.nacked.add(m)
                                sock.sendto(HDR.pack(NACK, m, 0.0), addr)
                    s.received.add(seq)
                    s.highest = max(s.highest, seq)
                sock.sendto(HDR.pack(ACK, seq, ts), addr)

            elif typ == FIN:
                sock.sendto(HDR.pack(FINACK, seq, 0.0), addr)
                s = sessions.pop(addr, None)
                if s is None:
                    continue                                       # duplicate FIN
                total = seq
                missing = [i for i in range(total) if i not in s.received]
                rec = dict(client=addr[0], port=addr[1], expected=total,
                           unique_received=len(s.received), missing_final=len(missing),
                           duplicates=s.dups, gaps_detected=s.gaps,
                           duration_s=round(time.time() - s.start, 3))
                with open(a.summary, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                print("[receiver] session %s:%d done -> %s" % (addr[0], addr[1], json.dumps(rec)), flush=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
