#!/usr/bin/env python3
"""Plot NSB ghost perf sweep metrics."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot NSB ghost perf sweep metrics")
    parser.add_argument("--csv", required=True, help="Input summary CSV")
    parser.add_argument("--out-dir", default="results/plots", help="Output directory for PNG plots")
    return parser.parse_args()


def to_float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_rows(csv_path: str):
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            nodes = to_float(row.get("nodes"))
            rate = to_float(row.get("rate"))
            avg_rtt = to_float(row.get("avg_rtt_s"))
            avg_cpu = to_float(row.get("avg_cpu_percent"))
            peak_mem = to_float(row.get("peak_memory_mb"))
            if nodes is None or rate is None:
                continue
            rows.append(
                {
                    "nodes": int(nodes),
                    "rate": int(rate),
                    "avg_rtt_s": avg_rtt,
                    "avg_cpu_percent": avg_cpu,
                    "peak_memory_mb": peak_mem,
                }
            )
    return rows


def plot_metric(rows, metric_key, y_label, title, out_path):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting.", file=sys.stderr)
        sys.exit(1)

    series = defaultdict(list)
    for row in rows:
        if row[metric_key] is None:
            continue
        series[row["nodes"]].append((row["rate"], row[metric_key]))

    if not series:
        print(f"No valid data for {metric_key}; skipping {out_path}", file=sys.stderr)
        return

    plt.figure(figsize=(10, 6))
    for nodes, points in sorted(series.items()):
        points.sort(key=lambda item: item[0])
        x = [item[0] for item in points]
        y = [item[1] for item in points]
        plt.plot(x, y, marker="o", label=f"nodes={nodes}")

    plt.xlabel("Message Rate (msg/s)")
    plt.ylabel(y_label)
    plt.title(title)
    plt.grid(True, linestyle="--", alpha=0.4)
    plt.legend(title="Node count")
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()


def main() -> None:
    args = parse_args()
    rows = load_rows(args.csv)
    if not rows:
        print("No rows found in CSV.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    plot_metric(
        rows,
        metric_key="avg_rtt_s",
        y_label="Average RTT (s)",
        title="Average RTT vs Message Rate",
        out_path=os.path.join(args.out_dir, "avg_rtt_vs_rate.png"),
    )
    plot_metric(
        rows,
        metric_key="avg_cpu_percent",
        y_label="Average CPU (%)",
        title="Average NSB Daemon CPU vs Message Rate",
        out_path=os.path.join(args.out_dir, "avg_cpu_vs_rate.png"),
    )
    plot_metric(
        rows,
        metric_key="peak_memory_mb",
        y_label="Peak Memory (MB)",
        title="Peak NSB Daemon Memory vs Message Rate",
        out_path=os.path.join(args.out_dir, "peak_memory_vs_rate.png"),
    )

    print(f"Plots written to: {args.out_dir}")


if __name__ == "__main__":
    main()
