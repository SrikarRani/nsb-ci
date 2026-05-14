#!/usr/bin/env python3
"""Validate optional performance thresholds against perf_summary.csv."""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path


THRESHOLD_ENV = {
    "avg_rtt_s": "MAX_AVG_RTT_S",
    "avg_cpu_percent": "MAX_AVG_CPU_PERCENT",
    "peak_memory_mb": "MAX_PEAK_MEMORY_MB",
    "drop_rate_percent": "MAX_DROP_RATE_PERCENT",
}


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value == "NA":
        return None
    return float(value)


def load_thresholds() -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for metric, env_name in THRESHOLD_ENV.items():
        raw = os.getenv(env_name, "").strip()
        if raw:
            thresholds[metric] = float(raw)
    return thresholds


def load_rows(csv_path: Path) -> list[dict[str, float | str | None]]:
    rows: list[dict[str, float | str | None]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            parsed = {
                "exit_code": (row.get("exit_code") or "").strip(),
                "nodes": parse_float(row.get("nodes")),
                "rate": parse_float(row.get("rate")),
                "sent": parse_float(row.get("sent")),
                "received": parse_float(row.get("received")),
                "dropped": parse_float(row.get("dropped")),
                "avg_rtt_s": parse_float(row.get("avg_rtt_s")),
                "avg_cpu_percent": parse_float(row.get("avg_cpu_percent")),
                "peak_memory_mb": parse_float(row.get("peak_memory_mb")),
                "drop_rate_percent": parse_float(row.get("drop_rate_percent")),
            }
            if parsed["nodes"] is None or parsed["rate"] is None:
                continue
            rows.append(parsed)  # type: ignore[arg-type]
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_perf_thresholds.py <perf_summary.csv>", file=sys.stderr)
        return 2

    csv_path = Path(sys.argv[1])
    if not csv_path.is_file():
        print(f"perf summary not found: {csv_path}", file=sys.stderr)
        return 2

    thresholds = load_thresholds()
    if not thresholds:
        print("No performance thresholds configured; skipping threshold enforcement.")
        return 0

    rows = load_rows(csv_path)
    if not rows:
        print("No usable performance rows found in summary CSV.", file=sys.stderr)
        return 1

    incomplete_rows = []
    failed_rows = []
    for row in rows:
        if row.get("exit_code") != "0":
            failed_rows.append(row)
        if any(row.get(field) is None for field in ("sent", "received", "dropped", "drop_rate_percent")):
            incomplete_rows.append(row)

    if failed_rows:
        print("One or more performance cases exited nonzero.", file=sys.stderr)
        for row in failed_rows:
            print(
                f"  - nodes={row['nodes']} rate={row['rate']} exit_code={row['exit_code']}",
                file=sys.stderr,
            )
        return 1

    if incomplete_rows:
        print("One or more performance rows are incomplete.", file=sys.stderr)
        for row in incomplete_rows:
            print(
                f"  - nodes={row['nodes']} rate={row['rate']} sent={row['sent']} received={row['received']}",
                file=sys.stderr,
            )
        return 1

    observed = {}
    for metric in THRESHOLD_ENV:
        values = [row[metric] for row in rows if row[metric] is not None]
        observed[metric] = max(values) if values else None

    print("Observed performance maxima:")
    for metric, value in observed.items():
        print(f"  {metric}={value if value is not None else 'NA'}")

    violations = []
    for metric, limit in thresholds.items():
        value = observed.get(metric)
        if value is None:
            violations.append(f"{metric}: no data available to compare against threshold {limit}")
        elif value > limit:
            violations.append(f"{metric}: observed {value} exceeds threshold {limit}")

    if violations:
        print("Performance threshold failures detected:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print("All configured performance thresholds passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
