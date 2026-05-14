#!/usr/bin/env python3
"""
NSB ghost simulator performance test client.

This is the same application-side load generator used for the ns-3 perf path,
but it runs against a ghost simulator that immediately posts fetched payloads
back through NSB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import string
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Dict, List, Set


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from nsb_client import NSBAppClient  # noqa: E402


def random_string(length: int = 24) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def make_data_message(msg_id: str, src: str, dest: str, payload: str) -> bytes:
    envelope = {
        "type": "data",
        "msg_id": msg_id,
        "src": src,
        "dest": dest,
        "ts_ms": now_ms(),
        "payload": payload,
        "payload_hash": payload_hash(payload),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decode_envelope(raw_payload) -> dict | None:
    try:
        text = raw_payload.decode("utf-8") if isinstance(raw_payload, bytes) else str(raw_payload)
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def percentile(values: List[float], p: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def progress_line(sent: int, received: int, elapsed_s: float) -> str:
    width = 30
    ratio = (received / sent) if sent else 0.0
    ratio = max(0.0, min(1.0, ratio))
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] recv={received}/{sent} ({ratio * 100.0:6.2f}%) elapsed={elapsed_s:6.1f}s"


@dataclass
class Metrics:
    rate: int
    duration_s: float
    sent: int = 0
    received: int = 0
    hash_failures: int = 0
    parse_failures: int = 0
    unexpected_msgs: int = 0
    rtt_s: List[float] = field(default_factory=list)

    def as_summary(self) -> dict:
        dropped = max(0, self.sent - self.received)
        drop_rate = (dropped / self.sent * 100.0) if self.sent else 0.0
        avg_rtt = mean(self.rtt_s) if self.rtt_s else 0.0
        return {
            "rate_msg_s": self.rate,
            "duration_s": self.duration_s,
            "sent": self.sent,
            "received": self.received,
            "drop_count": dropped,
            "drop_rate_percent": drop_rate,
            "hash_failures": self.hash_failures,
            "parse_failures": self.parse_failures,
            "unexpected_msgs": self.unexpected_msgs,
            "rtt_avg_s": avg_rtt,
            "rtt_p50_s": percentile(self.rtt_s, 50),
            "rtt_p95_s": percentile(self.rtt_s, 95),
            "rtt_p99_s": percentile(self.rtt_s, 99),
            "rtt_min_s": min(self.rtt_s) if self.rtt_s else 0.0,
            "rtt_max_s": max(self.rtt_s) if self.rtt_s else 0.0,
        }


def sender_worker(
    apps: list,
    metrics: Metrics,
    sent_tracker: Dict[str, dict],
    state_lock: threading.Lock,
    rate: int,
    duration_s: float,
    sender_done: threading.Event,
    sender_error: list[str],
) -> None:
    num_apps = len(apps)
    interval_s = 1.0 / rate
    start = time.perf_counter()
    end = start + duration_s
    next_send = start
    sequence = 0
    sent = 0
    last_src_idx = -1

    while time.perf_counter() < end:
        now = time.perf_counter()

        while now >= next_send and time.perf_counter() < end:
            src_idx = random.randint(0, num_apps - 1)
            if num_apps > 1:
                while src_idx == last_src_idx:
                    src_idx = random.randint(0, num_apps - 1)

            dst_idx = random.randint(0, num_apps - 1)
            while dst_idx == src_idx and num_apps > 1:
                dst_idx = random.randint(0, num_apps - 1)

            sender_host, sender_client = apps[src_idx]
            receiver_host, _ = apps[dst_idx]
            last_src_idx = src_idx

            payload_text = f"perf-{sequence}-{sender_host}-{random_string(12)}"
            msg_id = f"{rate}-{sequence}-{sender_host}-{receiver_host}-{now_ms()}"
            encoded = make_data_message(msg_id, sender_host, receiver_host, payload_text)
            p_hash = payload_hash(payload_text)

            send_ts = time.perf_counter()
            try:
                with state_lock:
                    sent_tracker[msg_id] = {"send_ts": send_ts, "payload_hash": p_hash}
                    metrics.sent += 1
                sender_client.send(receiver_host, encoded)
                time.sleep(0.0001)
                sent += 1
                sequence += 1
                next_send += interval_s
            except Exception as exc:  # pragma: no cover - runtime defensive path
                sender_error.append(str(exc))
                return

            if now < next_send:
                break

        remaining = next_send - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

    sender_done.set()
    elapsed = time.perf_counter() - start
    print(f"\n[SENDER] Done: {sent} messages in {elapsed:.2f}s ({sent / elapsed:.1f} msg/s actual)")


def main() -> None:
    parser = argparse.ArgumentParser(description="NSB ghost simulator performance test client")
    parser.add_argument("--nodes", type=int, default=10, help="Number of NSB host clients")
    parser.add_argument("--rate", type=int, default=10, help="Send rate in messages/second")
    parser.add_argument("--duration", type=float, default=10.0, help="How long the sender runs")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=20.0,
        help="Stop after N seconds with no received messages",
    )
    parser.add_argument("--server-host", default="127.0.0.1", help="NSB daemon address")
    parser.add_argument("--server-port", type=int, default=65432, help="NSB daemon port")
    parser.add_argument(
        "--receive-timeout",
        type=float,
        default=0.001,
        help="Seconds each centralized pull poll waits before moving to the next host",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    print(f"Initializing {args.nodes} app clients...")
    apps = []
    for i in range(args.nodes):
        host_id = f"host{i}"
        client = NSBAppClient(host_id, args.server_host, args.server_port)
        apps.append((host_id, client))
    print(f"App clients ready ({args.nodes} hosts).")

    metrics = Metrics(rate=args.rate, duration_s=args.duration)
    sent_tracker: Dict[str, dict] = {}
    received_ids: Set[str] = set()
    state_lock = threading.Lock()
    sender_done = threading.Event()
    sender_error: list[str] = []
    last_activity_ref = [time.perf_counter()]

    print(
        f"\n[RUN] rate={args.rate} msg/s | duration={args.duration}s | "
        f"idle_timeout={args.idle_timeout}s | nodes={args.nodes}"
    )

    sender_thread = threading.Thread(
        target=sender_worker,
        args=(apps, metrics, sent_tracker, state_lock, args.rate, args.duration, sender_done, sender_error),
        daemon=True,
    )
    sender_thread.start()

    loop_start = time.perf_counter()
    last_progress = 0.0
    next_recv_index = 0

    try:
        while True:
            now = time.perf_counter()
            with state_lock:
                sent_count = metrics.sent
                received_count = metrics.received
                hash_failures = metrics.hash_failures
                parse_failures = metrics.parse_failures
                unexpected_msgs = metrics.unexpected_msgs
                last_activity = last_activity_ref[0]

            for _ in range(len(apps)):
                host_id, client = apps[next_recv_index]
                next_recv_index = (next_recv_index + 1) % len(apps)
                received = client.receive(timeout=args.receive_timeout)
                if not received:
                    continue

                envelope = decode_envelope(received.payload)
                with state_lock:
                    if envelope is None:
                        metrics.parse_failures += 1
                        continue

                    msg_id = envelope.get("msg_id")
                    if not msg_id or envelope.get("type") != "data":
                        metrics.unexpected_msgs += 1
                        continue

                    if msg_id not in sent_tracker or msg_id in received_ids:
                        continue

                    if envelope.get("dest") != host_id:
                        metrics.unexpected_msgs += 1
                        continue

                    payload_text = envelope.get("payload", "")
                    expected_hash = envelope.get("payload_hash", "")
                    if payload_hash(payload_text) != expected_hash:
                        metrics.hash_failures += 1
                        continue
                    if expected_hash != sent_tracker[msg_id]["payload_hash"]:
                        metrics.hash_failures += 1
                        continue

                    rtt = time.perf_counter() - sent_tracker[msg_id]["send_ts"]
                    received_ids.add(msg_id)
                    metrics.received += 1
                    metrics.rtt_s.append(rtt)
                    last_activity_ref[0] = time.perf_counter()

            now = time.perf_counter()
            if now - last_progress >= 0.2:
                elapsed = now - loop_start
                print(f"\r{progress_line(sent_count, received_count, elapsed)}", end="", flush=True)
                last_progress = now

            if sender_done.is_set() and sent_count > 0 and received_count >= sent_count:
                break

            idle_s = now - last_activity
            if sender_done.is_set() and idle_s >= args.idle_timeout:
                print(
                    f"\n[TIMEOUT] No messages for {idle_s:.1f}s "
                    f"(received {received_count}/{sent_count}). Stopping."
                )
                break

            if sender_error:
                print(f"\n[SENDER ERROR] {sender_error[0]}")
                break

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")

    sender_thread.join(timeout=5)

    elapsed = time.perf_counter() - loop_start
    print(f"\r{progress_line(metrics.sent, metrics.received, elapsed)}")

    summary = metrics.as_summary()
    print("\n==== SUMMARY ====")
    print(f"  rate:     {summary['rate_msg_s']} msg/s")
    print(f"  duration: {summary['duration_s']}s")
    print(f"  sent:     {summary['sent']}")
    print(f"  received: {summary['received']}")
    print(f"  dropped:  {summary['drop_count']} ({summary['drop_rate_percent']:.2f}%)")
    print(
        f"  hash_err: {summary['hash_failures']}  parse_err: {summary['parse_failures']}  "
        f"unexpected: {summary['unexpected_msgs']}"
    )
    if summary["rtt_avg_s"] > 0:
        print(
            f"  rtt:      avg={summary['rtt_avg_s']:.4f}s  "
            f"p50={summary['rtt_p50_s']:.4f}s  p95={summary['rtt_p95_s']:.4f}s  "
            f"p99={summary['rtt_p99_s']:.4f}s"
        )
        print(
            f"            min={summary['rtt_min_s']:.4f}s  max={summary['rtt_max_s']:.4f}s"
        )
    print("=================")

    strict_failure = any(
        [
            bool(sender_error),
            summary["sent"] == 0,
            summary["received"] != summary["sent"],
            summary["hash_failures"] > 0,
            summary["parse_failures"] > 0,
            summary["unexpected_msgs"] > 0,
        ]
    )
    if strict_failure:
        print("[RESULT] FAIL")
        raise SystemExit(1)

    print("[RESULT] PASS")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
