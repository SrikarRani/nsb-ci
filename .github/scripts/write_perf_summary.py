#!/usr/bin/env python3
"""Write a readable Markdown summary for NSB perf results."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write GitHub summary for NSB perf results")
    parser.add_argument("--csv", required=True, help="Path to perf_summary.csv")
    parser.add_argument("--output", required=True, help="Path to Markdown output file")
    parser.add_argument("--title", default="NSB Performance Summary", help="Summary title")
    return parser.parse_args()


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    output_path = Path(args.output)

    if not csv_path.is_file():
        output_path.write_text(
            f"## {args.title}\n\nNo perf summary CSV was produced at `{csv_path}`.\n",
            encoding="utf-8",
        )
        return 0

    rows = read_rows(csv_path)
    total = len(rows)
    passed = sum(1 for row in rows if row.get("exit_code") == "0")
    failed = total - passed

    lines = [f"## {args.title}", ""]
    lines.append(f"- Cases run: {total}")
    lines.append(f"- Passed: {passed}")
    lines.append(f"- Failed: {failed}")
    lines.append("")
    lines.append("### Latency Metric Definitions")
    lines.append("")
    lines.append(
        "- `Avg RTT (s)`: App-visible end-to-end roundtrip time from app send to app receive. "
        "This includes NSB handling, simulator turnaround, and any harness-side delay."
    )
    lines.append(
        "- `Daemon Roundtrip (s)`: Daemon-visible roundtrip time from `t_daemon_send_ingress` "
        "to `t_daemon_receive_egress`. This is the closest daemon-side equivalent to RTT and "
        "includes time waiting in NSB before simulator fetch, simulator turnaround, and time "
        "waiting in NSB before app receive."
    )
    lines.append(
        "- `NSB Residence (s)`: Time spent inside NSB only: "
        "(`t_daemon_fetch_egress - t_daemon_send_ingress`) + "
        "(`t_daemon_receive_egress - t_daemon_post_ingress`). This excludes simulator turnaround."
    )
    lines.append(
        "- `Sim Turnaround (s)`: Time outside NSB between daemon handoff to the simulator and "
        "the simulator returning the message: `t_daemon_post_ingress - t_daemon_fetch_egress`."
    )
    lines.append("")

    if not rows:
        lines.append("No performance rows were produced.")
    else:
        lines.extend(
            [
                "| Case | Status | Sent | Received | Drop % | Errors (hash/parse/unexpected) | Avg RTT (s) | Daemon Roundtrip (s) | NSB Residence (s) | Sim Turnaround (s) | Avg CPU % | Peak Mem (MB) |",
                "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in rows:
            case = (
                f"n{row.get('nodes', 'NA')} @ r{row.get('rate', 'NA')}"
                f" for {row.get('duration_s', 'NA')}s"
            )
            complete = all(
                row.get(field, "NA") != "NA"
                for field in (
                    "sent",
                    "received",
                    "dropped",
                    "drop_rate_percent",
                    "trace_samples",
                    "daemon_roundtrip_avg_s",
                    "nsb_residence_avg_s",
                    "sim_turnaround_avg_s",
                )
            )
            status = (
                "PASS"
                if row.get("exit_code") == "0" and complete and row.get("trace_samples") != "0"
                else "FAIL"
            )
            errors = (
                f"{row.get('hash_failures', 'NA')}/"
                f"{row.get('parse_failures', 'NA')}/"
                f"{row.get('unexpected_msgs', 'NA')}"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        case,
                        status,
                        row.get("sent", "NA"),
                        row.get("received", "NA"),
                        row.get("drop_rate_percent", "NA"),
                        errors,
                        row.get("avg_rtt_s", "NA"),
                        row.get("daemon_roundtrip_avg_s", "NA"),
                        row.get("nsb_residence_avg_s", "NA"),
                        row.get("sim_turnaround_avg_s", "NA"),
                        row.get("avg_cpu_percent", "NA"),
                        row.get("peak_memory_mb", "NA"),
                    ]
                )
                + " |"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
