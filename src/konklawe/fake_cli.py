"""Fake provider CLI: replays a recorded stream so tests and demos use no subscription limits.

Usage: python -m konklawe.fake_cli --fixture FILE [--delay-ms N] [--exit-code N]
       [--stderr TEXT] [--hang] [--report-env NAME ...] [--spawn-child] [--ignore-sigterm]
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m konklawe.fake_cli")
    parser.add_argument("--fixture", type=Path, help="recorded stdout to replay line by line")
    parser.add_argument("--delay-ms", type=int, default=0, help="pause before each line")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stderr", action="append", default=[], help="line to write to stderr")
    parser.add_argument("--hang", action="store_true", help="never exit (timeout tests)")
    parser.add_argument(
        "--report-env",
        nargs="+",
        default=[],
        metavar="NAME",
        help="print a JSON line telling which of these variables are set",
    )
    parser.add_argument(
        "--spawn-child",
        action="store_true",
        help="start a long-running child process and print its pid (process-tree tests)",
    )
    parser.add_argument(
        "--ignore-sigterm", action="store_true", help="ignore SIGTERM so only SIGKILL works"
    )
    return parser.parse_args(argv)


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    # Like the real CLI, consume the prompt first so the writer never blocks on a full pipe.
    if not sys.stdin.isatty():
        sys.stdin.read()

    if args.report_env:
        emit(json.dumps({"type": "fake_env", "set": {n: n in os.environ for n in args.report_env}}))

    if args.spawn_child:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        emit(json.dumps({"type": "fake_child", "pid": child.pid, "parent_pid": os.getpid()}))

    if args.fixture:
        with args.fixture.open(encoding="utf-8") as fixture:
            for line in fixture:
                if args.delay_ms:
                    time.sleep(args.delay_ms / 1000)
                emit(line.rstrip("\r\n"))

    for text in args.stderr:
        sys.stderr.write(text + "\n")
    sys.stderr.flush()

    while args.hang:
        time.sleep(3600)
    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
