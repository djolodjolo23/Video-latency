#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

METRICS_DONE = "Metrics window complete"
DEFAULT_METRICS_HEADER = (
    "timestamp,session_id,expected_clients,connected_clients,duration_sec,"
    "avg_sys_cpu_pct,avg_sys_mem_pct,avg_proc_cpu_pct,avg_proc_rss_mb,"
    "avg_net_rx_kbps,avg_net_tx_kbps,samples"
)
NET_COUNTER_COLUMNS = [
    "net_counter_in_bytes",
    "net_counter_out_bytes",
    "net_counter_in_packets",
    "net_counter_out_packets",
]


def _counter_script_path(args: argparse.Namespace) -> Path:
    if args.net_counter_script:
        return Path(args.net_counter_script)
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "scripts" / "net_counters.sh"


def _run_net_counter(args: argparse.Namespace, action: str) -> str | None:
    if not args.net_iface or not args.net_clients:
        return None
    script_path = _counter_script_path(args)
    if not script_path.exists():
        print(f"[sweep] net counter script not found at {script_path}")
        return None
    cmd = [str(script_path), action]
    if action == "setup":
        cmd += ["--iface", args.net_iface, "--clients", args.net_clients]
    if args.net_sudo and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"[sweep] net counter {action} failed: {exc.stdout}")
        return None


def _parse_net_summary(output: str | None) -> dict[str, int]:
    if not output:
        return {}
    values: dict[str, int] = {}
    for token in output.split():
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        try:
            values[key] = int(raw)
        except ValueError:
            continue
    return values


def _metrics_output_path(server_dir: Path, server_args: list[str]) -> Path:
    metrics_output = None
    if "--metrics-output" in server_args:
        idx = server_args.index("--metrics-output")
        if idx + 1 < len(server_args):
            metrics_output = server_args[idx + 1]
    metrics_output = metrics_output or "server_metrics.csv"
    path = Path(metrics_output)
    if not path.is_absolute():
        return server_dir / path
    return path


def _update_metrics_csv(metrics_path: Path, summary: dict[str, int]) -> None:
    if not summary or not metrics_path.exists():
        return
    content = metrics_path.read_text().splitlines()
    if not content:
        return
    if "timestamp" not in content[0]:
        content.insert(0, DEFAULT_METRICS_HEADER)
    header_cols = content[0].split(",")
    for col in NET_COUNTER_COLUMNS:
        if col not in header_cols:
            header_cols.append(col)
    content[0] = ",".join(header_cols)

    idx = len(content) - 1
    while idx > 0 and not content[idx].strip():
        idx -= 1
    if idx <= 0:
        return
    row_cols = content[idx].split(",")
    while len(row_cols) < len(header_cols):
        row_cols.append("")

    mapping = {
        "net_counter_in_bytes": summary.get("in_bytes", 0),
        "net_counter_out_bytes": summary.get("out_bytes", 0),
        "net_counter_in_packets": summary.get("in_packets", 0),
        "net_counter_out_packets": summary.get("out_packets", 0),
    }
    for key, value in mapping.items():
        try:
            col_idx = header_cols.index(key)
        except ValueError:
            continue
        row_cols[col_idx] = str(value)
    content[idx] = ",".join(row_cols)
    metrics_path.write_text("\n".join(content) + "\n")


def run_server(
    expected: int, args: argparse.Namespace, metrics_path: Path
) -> tuple[int, dict[str, int]]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    server_dir = Path(__file__).resolve().parent
    server_path = server_dir / "server.py"
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
    net_summary: dict[str, int] = {}

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
            if args.net_iface and args.net_clients:
                net_summary = _parse_net_summary(_run_net_counter(args, "summary"))
            proc.send_signal(signal.SIGINT)
            stop_sent = True

    code = proc.wait()
    if net_summary:
        _update_metrics_csv(metrics_path, net_summary)
    return code, net_summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run WebRTC server metrics for a range of expected client counts."
    )
    parser.add_argument("--start", type=int, default=1, help="Starting expected client count")
    parser.add_argument("--end", type=int, default=50, help="Ending expected client count (inclusive)")
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
    parser.add_argument("--net-iface", default=None, help="Interface name for net counters (e.g. eth0)")
    parser.add_argument(
        "--net-clients",
        default=None,
        help="Comma-separated client IPs to track with net counters",
    )
    parser.add_argument(
        "--net-counter-script",
        default=None,
        help="Path to scripts/net_counters.sh (defaults to repo scripts/)",
    )
    parser.add_argument(
        "--net-sudo",
        action="store_true",
        help="Run net counter commands through sudo",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Working directory where the server is launched (defaults to the webrtc folder)",
    )
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    server_dir = Path(__file__).resolve().parent
    base_dir = Path(args.workdir) if args.workdir else server_dir
    server_args = list(args.server_args)
    if server_args[:1] == ["--"]:
        server_args = server_args[1:]
    metrics_path = _metrics_output_path(base_dir, server_args)

    if args.net_iface and args.net_clients:
        _run_net_counter(args, "setup")

    for expected in range(args.start, args.end + 1):
        if args.net_iface and args.net_clients:
            _run_net_counter(args, "reset")
        code, _ = run_server(expected, args, metrics_path)
        if code not in (0, -signal.SIGINT):
            print(f"[sweep] server exited with code {code}; stopping")
            sys.exit(code if code != 0 else 1)
        time.sleep(1)


if __name__ == "__main__":
    main()
