#!/usr/bin/env python3
"""Repeatable offline transport benchmark for CI and local comparisons.

The fixture measures the limiter/telemetry plumbing only.  It never contacts
an upstream service and must not be read as a supplier speed claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cnequity.diagnostics.metrics import check_offline_benchmark, run_offline_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON result to this path")
    parser.add_argument("--requests", type=int, default=8, dest="requests_per_source")
    parser.add_argument("--concurrency", type=int, default=2, dest="concurrency_limit")
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument("--latency", type=float, default=0.001, dest="latency_seconds")
    parser.add_argument("--retry-every", type=int, default=0)
    parser.add_argument(
        "--check",
        "--ci",
        dest="check",
        action="store_true",
        help="apply deterministic CI thresholds and exit non-zero on a breach",
    )
    parser.add_argument(
        "--max-elapsed-seconds",
        "--max-elapsed",
        type=float,
        default=None,
        help="maximum fixture wall time in check mode (default: 10 seconds)",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help="maximum observed in-flight fixture requests in check mode",
    )
    parser.add_argument(
        "--min-throughput-requests-per-second",
        "--min-throughput",
        type=float,
        default=None,
        dest="min_throughput_requests_per_second",
        help="minimum fixture throughput in check mode (default: 0)",
    )
    args = parser.parse_args()
    try:
        result = run_offline_benchmark(
            requests_per_source=args.requests_per_source,
            concurrency_limit=args.concurrency_limit,
            payload_bytes=args.payload_bytes,
            latency_seconds=args.latency_seconds,
            retry_every=args.retry_every,
            max_elapsed_seconds=(
                10.0 if args.max_elapsed_seconds is None else args.max_elapsed_seconds
            ),
            max_concurrency=args.max_concurrency,
            min_throughput_requests_per_second=(
                0.0
                if args.min_throughput_requests_per_second is None
                else args.min_throughput_requests_per_second
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.check:
        failures = check_offline_benchmark(
            result,
            max_elapsed_seconds=args.max_elapsed_seconds,
            max_concurrency=args.max_concurrency,
            min_throughput_requests_per_second=args.min_throughput_requests_per_second,
        )
        if failures:
            for failure in failures:
                print(f"benchmark check failed: {failure}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
