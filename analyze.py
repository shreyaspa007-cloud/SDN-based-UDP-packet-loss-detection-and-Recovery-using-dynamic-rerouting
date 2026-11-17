#!/usr/bin/env python3
"""
Offline analysis of an experiment directory produced by run_experiment.py.

    python3 analyze.py results/<timestamp>

Per run (mode x fault level) and per client it computes
    pre_loss      first-attempt loss % before the fault
    post_loss     first-attempt loss % from the fault until the end of the run
    steady_loss   first-attempt loss % after recovery (n/a if the run never recovered)
    residual      % of packets never delivered even after retransmissions
    lat_mean/p95  delivery latency (first send -> ACK) of packets sent after the fault, ms
    retx_overhead retransmissions per packet sent after the fault, %
    detect_s      fault -> controller reroute            (failover / detection time)
    recovery_s    fault -> first of K_STABLE consecutive packets delivered on the first attempt
and writes summary.csv, summary.md and PNG plots.
"""
import csv
import glob
import json
import os
import sys

K_STABLE = 60          # consecutive first-attempt successes that define "recovered" (1.2 s at 50 pps)
BIN_S = 1.0


def load_csv(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(dict(seq=int(r["seq"]), first_send=float(r["first_send"]),
                             ack=float(r["ack_time"]) if r["ack_time"] else None,
                             tries=int(r["tries"]), status=r["status"]))
    rows.sort(key=lambda r: r["first_send"])
    return rows


def first_ok(r):
    return r["status"] == "ok" and r["tries"] == 0


def loss_pct(rows):
    return None if not rows else 100.0 * sum(1 for r in rows if not first_ok(r)) / len(rows)


def percentile(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[int(round(p * (len(vals) - 1)))]


def find_recovery(rows, fault_ts):
    after = [r for r in rows if r["first_send"] >= fault_ts]
    for i in range(0, len(after) - K_STABLE + 1):
        if all(first_ok(r) for r in after[i:i + K_STABLE]):
            return after[i]["first_send"]
    return None


def metrics(rows, meta):
    fault_ts = meta["fault_ts"]
    rec_ts = find_recovery(rows, fault_ts)
    pre = [r for r in rows if r["first_send"] < fault_ts]
    post = [r for r in rows if r["first_send"] >= fault_ts]
    steady = [r for r in rows if rec_ts is not None and r["first_send"] >= rec_ts]
    lat = [(r["ack"] - r["first_send"]) * 1000 for r in post if r["ack"] is not None]
    return dict(
        sent=len(rows),
        pre_loss=loss_pct(pre), post_loss=loss_pct(post), steady_loss=loss_pct(steady),
        residual=100.0 * sum(1 for r in rows if r["status"] != "ok") / max(1, len(rows)),
        lat_mean=(sum(lat) / len(lat)) if lat else None, lat_p95=percentile(lat, 0.95),
        retx_overhead=(100.0 * sum(r["tries"] for r in post) / len(post)) if post else None,
        detect_s=(meta["reroute_ts"] - fault_ts) if meta.get("reroute_ts") else None,
        recovery_s=(rec_ts - fault_ts) if rec_ts else None,
    )


def fmt(x, nd=1):
    return "n/a" if x is None else ("%.*f" % (nd, x))


def load_runs(root):
    runs = []
    for mpath in sorted(glob.glob(os.path.join(root, "*", "meta.json"))):
        d = os.path.dirname(mpath)
        meta = json.load(open(mpath))
        clients = {}
        for c in sorted(glob.glob(os.path.join(d, "h*.csv"))):
            clients[os.path.basename(c)[:-4]] = load_csv(c)
        if clients:
            runs.append((meta, clients))
    return runs


def timeline(rows, t0):
    bins = {}
    for r in rows:
        b = int((r["first_send"] - t0) // BIN_S)
        n, l = bins.get(b, (0, 0))
        bins[b] = (n + 1, l + (0 if first_ok(r) else 1))
    xs = sorted(bins)
    return [x * BIN_S for x in xs], [100.0 * bins[x][1] / bins[x][0] for x in xs]


def plots(runs, root):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping plots")
        return
    # 1. timeline per condition (all modes on one chart)
    conds = {}
    for meta, clients in runs:
        conds.setdefault((meta["fault"], meta["level"]), []).append((meta, clients))
    colors = {"static": "tab:red", "stats_only": "tab:orange", "dynamic": "tab:green"}
    for (fault, level), items in conds.items():
        plt.figure(figsize=(9, 4))
        for meta, clients in items:
            rows = clients.get("h1") or list(clients.values())[0]
            t0 = rows[0]["first_send"]
            xs, ys = timeline(rows, t0)
            c = colors.get(meta["mode"], None)
            plt.plot(xs, ys, marker="o", ms=3, label=meta["mode"], color=c)
            if meta.get("reroute_ts"):
                plt.axvline(meta["reroute_ts"] - t0, color=c, ls=":", alpha=.8)
        plt.axvline(items[0][0]["fault_ts"] - t0, color="k", ls="--", label="fault injected")
        plt.xlabel("time since start of transfer (s)")
        plt.ylabel("first-attempt packet loss per second (%)")
        plt.title("Packet loss over time - %s %s%%  (dotted = controller reroute)" % (fault, level))
        plt.legend()
        plt.grid(alpha=.3)
        plt.tight_layout()
        plt.savefig(os.path.join(root, "timeline_%s%s.png" % (fault, level)), dpi=130)
        plt.close()

    # 2. summary bars: post-fault loss, post-fault latency, recovery time
    levels = sorted(conds, key=lambda k: (k[0], k[1]))
    modes = [m for m in ("static", "stats_only", "dynamic") if any(r[0]["mode"] == m for r in runs)]
    rows_by = {}
    for meta, clients in runs:
        rows = clients.get("h1") or list(clients.values())[0]
        rows_by[(meta["fault"], meta["level"], meta["mode"])] = metrics(rows, meta)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    titles = [("post_loss", "First-attempt loss after fault (%)"),
              ("lat_mean", "Mean delivery latency after fault (ms)"),
              ("recovery_s", "Recovery time (s)  [no bar = never recovered]")]
    width = 0.8 / max(1, len(modes))
    for ax, (key, title) in zip(axes, titles):
        for i, m in enumerate(modes):
            vals = [rows_by.get((f, l, m), {}).get(key) for f, l in levels]
            ax.bar([x + i * width for x in range(len(levels))], [v or 0 for v in vals], width,
                   label=m, color=colors.get(m))
        ax.set_xticks([x + width * (len(modes) - 1) / 2 for x in range(len(levels))])
        ax.set_xticklabels(["%s %s%%" % (f, l) for f, l in levels])
        ax.set_title(title, fontsize=10)
        ax.grid(axis="y", alpha=.3)
    axes[0].legend()
    plt.tight_layout()
    plt.savefig(os.path.join(root, "comparison.png"), dpi=130)
    plt.close()


def analyze(root):
    runs = load_runs(root)
    if not runs:
        print("no runs found in", root)
        return
    cols = ["fault", "level", "mode", "client", "sent", "pre_loss", "post_loss", "steady_loss", "residual",
            "lat_mean", "lat_p95", "retx_overhead", "detect_s", "recovery_s"]
    table = []
    for meta, clients in runs:
        for name, rows in sorted(clients.items()):
            m = metrics(rows, meta)
            table.append(dict(fault=meta["fault"], level=meta["level"], mode=meta["mode"], client=name, **m))
    table.sort(key=lambda r: (r["fault"], r["level"], ["static", "stats_only", "dynamic"].index(r["mode"])
                              if r["mode"] in ("static", "stats_only", "dynamic") else 9, r["client"]))

    with open(os.path.join(root, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in table:
            w.writerow([r[c] for c in cols])

    heads = ["fault", "lvl%", "mode", "cli", "pre loss%", "post loss%", "steady loss%", "residual%",
             "lat mean ms", "lat p95 ms", "retx %", "detect s", "recovery s"]
    lines = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
    for r in table:
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["fault"], r["level"], r["mode"], r["client"], fmt(r["pre_loss"]), fmt(r["post_loss"]),
            fmt(r["steady_loss"]), fmt(r["residual"], 2), fmt(r["lat_mean"]), fmt(r["lat_p95"]),
            fmt(r["retx_overhead"]), fmt(r["detect_s"], 2), fmt(r["recovery_s"], 2)))
    md = "\n".join(lines)
    with open(os.path.join(root, "summary.md"), "w") as f:
        f.write(md + "\n")
    print(md)
    plots(runs, root)
    print("\nSaved summary.csv, summary.md and PNG plots in", root)


if __name__ == "__main__":
    analyze(sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("results/*"))[-1])
