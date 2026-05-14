#!/usr/bin/env python3
"""Run a curated set of ghost-simulator perf cases and write a summary CSV."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from pathlib import Path


SUMMARY_FIELDS = [
    "nodes",
    "rate",
    "duration_s",
    "sent",
    "received",
    "dropped",
    "drop_rate_percent",
    "avg_rtt_s",
    "p50_rtt_s",
    "p95_rtt_s",
    "p99_rtt_s",
    "min_rtt_s",
    "max_rtt_s",
    "avg_cpu_percent",
    "peak_memory_mb",
    "run_dir",
    "exit_code",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run curated NSB ghost perf cases")
    parser.add_argument("--repo-root", required=True, help="Path to the NSB repo root")
    parser.add_argument("--capture-script", required=True, help="Path to the capture script")
    parser.add_argument("--cases", required=True, help="Comma-separated cases like 5:10,25:50")
    parser.add_argument("--duration", required=True, help="Duration in seconds for each case")
    parser.add_argument("--output-root", required=True, help="Output root for perf artifacts")
    parser.add_argument("--summary-csv", required=True, help="Path to output summary CSV")
    parser.add_argument(
        "--capture-arg",
        action="append",
        default=[],
        help="Extra arg forwarded to the capture script; repeatable",
    )
    return parser.parse_args()


def parse_cases(raw_cases: str) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for item in raw_cases.split(","):
        text = item.strip()
        if not text:
            continue
        if ":" not in text:
            raise ValueError(f"Invalid case format: {text}")
        nodes, rate = [part.strip() for part in text.split(":", 1)]
        if not nodes or not rate:
            raise ValueError(f"Invalid case format: {text}")
        cases.append((nodes, rate))
    if not cases:
        raise ValueError("No perf cases provided")
    return cases


def extract_value(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else "NA"


def parse_metrics(run_dir: Path) -> dict[str, str]:
    metrics = {
        "sent": "NA",
        "received": "NA",
        "dropped": "NA",
        "drop_rate_percent": "NA",
        "avg_rtt_s": "NA",
        "p50_rtt_s": "NA",
        "p95_rtt_s": "NA",
        "p99_rtt_s": "NA",
        "min_rtt_s": "NA",
        "max_rtt_s": "NA",
        "avg_cpu_percent": "NA",
        "peak_memory_mb": "NA",
    }

    parsed_metrics = run_dir / "parsed_test_metrics.txt"
    if parsed_metrics.is_file():
        text = parsed_metrics.read_text(encoding="utf-8", errors="replace")
        metrics["sent"] = extract_value(r"^\s*sent:\s+([0-9]+)", text)
        metrics["received"] = extract_value(r"^\s*received:\s+([0-9]+)", text)
        metrics["dropped"] = extract_value(r"^\s*dropped:\s+([0-9]+)", text)
        metrics["drop_rate_percent"] = extract_value(r"^\s*dropped:.*\(([0-9.]+)%\)", text)
        metrics["avg_rtt_s"] = extract_value(r"avg=([0-9.]+)s", text)
        metrics["p50_rtt_s"] = extract_value(r"p50=([0-9.]+)s", text)
        metrics["p95_rtt_s"] = extract_value(r"p95=([0-9.]+)s", text)
        metrics["p99_rtt_s"] = extract_value(r"p99=([0-9.]+)s", text)
        metrics["min_rtt_s"] = extract_value(r"min=([0-9.]+)s", text)
        metrics["max_rtt_s"] = extract_value(r"max=([0-9.]+)s", text)

    resource_stats = run_dir / "daemon_resource_stats.txt"
    if resource_stats.is_file():
        text = resource_stats.read_text(encoding="utf-8", errors="replace")
        metrics["avg_cpu_percent"] = extract_value(r"^cpu_percent_avg=([0-9.]+)", text)
        metrics["peak_memory_mb"] = extract_value(r"^rss_mb_max=([0-9.]+)", text)

    return metrics


def find_run_dir(output_root: Path, tag: str) -> Path | None:
    matches = sorted(output_root.glob(f"*_{tag}"), reverse=True)
    for match in matches:
        if match.is_dir():
            return match
    return None


def run_case(
    repo_root: Path,
    capture_script: Path,
    output_root: Path,
    duration: str,
    nodes: str,
    rate: str,
    capture_args: list[str],
) -> dict[str, str]:
    tag = f"n{nodes}_r{rate}"
    command = [
        "bash",
        str(capture_script),
        "--output-root",
        str(output_root),
        "--tag",
        tag,
        *capture_args,
        "--",
        "--rate",
        rate,
        "--duration",
        duration,
        "--nodes",
        nodes,
    ]

    print(f"\n=== Curated ghost perf case: nodes={nodes}, rate={rate} ===", flush=True)
    completed = subprocess.run(command, cwd=repo_root)
    run_dir = find_run_dir(output_root, tag)

    row = {field: "NA" for field in SUMMARY_FIELDS}
    row["nodes"] = nodes
    row["rate"] = rate
    row["duration_s"] = duration
    row["exit_code"] = str(completed.returncode)
    row["run_dir"] = str(run_dir) if run_dir else "NA"

    if run_dir is not None:
        row.update(parse_metrics(run_dir))

    return row


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    capture_script = Path(args.capture_script).resolve()
    output_root = Path(args.output_root).resolve()
    summary_csv = Path(args.summary_csv).resolve()

    output_root.mkdir(parents=True, exist_ok=True)
    summary_csv.parent.mkdir(parents=True, exist_ok=True)

    rows = [
        run_case(repo_root, capture_script, output_root, args.duration, nodes, rate, args.capture_arg)
        for nodes, rate in parse_cases(args.cases)
    ]

    with summary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCurated ghost perf suite complete. Summary CSV: {summary_csv}")
    failed = [row for row in rows if row["exit_code"] != "0"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
