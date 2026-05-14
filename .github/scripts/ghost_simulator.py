#!/usr/bin/env python3
"""Minimal ghost simulator for NSB CI perf runs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from nsb_client import NSBSimClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a ghost NSB simulator client")
    parser.add_argument("--identifier", default="ghost", help="Simulator client identifier")
    parser.add_argument("--server-host", default="127.0.0.1", help="NSB daemon host")
    parser.add_argument("--server-port", type=int, default=65432, help="NSB daemon port")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python log level",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[ghost-sim] %(message)s",
    )
    logger = logging.getLogger("ghost-sim")
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    sim = NSBSimClient(args.identifier, args.server_host, args.server_port)
    logger.info(
        "Connected ghost simulator '%s' to %s:%s",
        args.identifier,
        args.server_host,
        args.server_port,
    )

    relayed = 0
    while not stop_event.is_set():
        try:
            msg = await sim.listen()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # pragma: no cover - defensive for CI runtime
            logger.exception("Simulator listen failed: %s", exc)
            return 1

        if not msg:
            continue

        payload = msg.payload if getattr(msg, "payload", None) else b""
        sim.post(msg.src_id, msg.dest_id, payload)
        relayed += 1
        if relayed % 50 == 0:
            logger.info("Relayed %s payloads", relayed)

    logger.info("Stopping ghost simulator after relaying %s payloads", relayed)
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
