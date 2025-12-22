#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

METRICS_DONE = "Metrics window complete"


def run_server(expected: int, args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    server_dir = Path(__file__).resolve().parent
    server_path = server_dir / "server" / "streamer.py"
    cmd = [
        sys.executable,
        "-u",
        str(server_path),
        "--expected-clients",
        str(expected),
        "--connect-timeout",
        str(args.connect_timeout),
        "--metrics-duration",
        str(args.metrics_duration),
        "--client-timeout",
        str(args.client_timeout),
    ]
    server_args = list(args.server_args)
    if server_args[:1] == ["--"]:
        server_args = server_args[1:]
    cmd.extend(server_args)

    print(f"[sweep] starting expected_clients={expected}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=args.workdir or str(server_dir),
    )

    metrics_done = False
    stop_sent = False

    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        sys.stdout.write(f"[{expected}] {line}")
        sys.stdout.flush()

        if METRICS_DONE in line:
            metrics_done = True
        if metrics_done and not stop_sent:
            if args.stop_delay > 0:
                time.sleep(args.stop_delay)
            proc.send_signal(signal.SIGINT)
            stop_sent = True

    return proc.wait()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run LL-HLS server metrics for a range of expected client counts."
    )
    parser.add_argument("--start", type=int, default=1, help="Starting expected client count")
    parser.add_argument("--end", type=int, default=30, help="Ending expected client count (inclusive)")
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=86400.0,
        help="Seconds to wait for clients before aborting the server",
    )
    parser.add_argument(
        "--metrics-duration",
        type=float,
        default=30.0,
        help="Seconds to collect server metrics once all clients connect",
    )
    parser.add_argument(
        "--stop-delay",
        type=float,
        default=1.0,
        help="Seconds to wait after metrics complete before stopping the server",
    )
    parser.add_argument(
        "--client-timeout",
        type=float,
        default=10.0,
        help="Seconds of inactivity before a client is considered disconnected",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Working directory where the server is launched (defaults to the ll-hls folder)",
    )
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    for expected in range(args.start, args.end + 1):
        code = run_server(expected, args)
        if code not in (0, -signal.SIGINT):
            print(f"[sweep] server exited with code {code}; stopping")
            sys.exit(code if code != 0 else 1)
        time.sleep(1)


if __name__ == "__main__":
    main()
