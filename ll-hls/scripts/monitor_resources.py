#!/usr/bin/env python3
"""Lightweight system monitor to watch CPU/memory/network while streaming."""
from __future__ import annotations

import argparse
import datetime as dt
import time
from typing import Optional

import psutil


def fmt_bytes_per_s(value: float) -> str:
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def find_process(name: str) -> Optional[psutil.Process]:
    name = name.lower()
    for proc in psutil.process_iter(attrs=["name"]):
        if proc.info.get("name", "").lower() == name:
            return proc
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor CPU/memory/network usage while streaming")
    parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds (default: 1.0)")
    parser.add_argument("--iface", action="append", help="Network interface to monitor (can be repeated); defaults to all")
    parser.add_argument("--pid", type=int, help="Track a specific process ID (e.g., the ffmpeg PID)")
    parser.add_argument("--proc-name", default="ffmpeg", help="Fallback process name to track if --pid is not set (default: ffmpeg)")
    args = parser.parse_args()

    psutil.cpu_percent(interval=None)
    prev_net = psutil.net_io_counters(pernic=True)

    tracked_proc: Optional[psutil.Process] = None
    if args.pid:
        try:
            tracked_proc = psutil.Process(args.pid)
        except psutil.NoSuchProcess:
            print(f"[warn] PID {args.pid} not found; will try name match instead")
    if tracked_proc is None and args.proc_name:
        tracked_proc = find_process(args.proc_name)

    try:
        while True:
            time.sleep(args.interval)

            now = dt.datetime.now().strftime("%H:%M:%S")
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()

            net = psutil.net_io_counters(pernic=True)
            rx_total = tx_total = 0.0
            ifaces = args.iface or list(net.keys())
            for iface in ifaces:
                if iface not in net or iface not in prev_net:
                    continue
                rx_delta = net[iface].bytes_recv - prev_net[iface].bytes_recv
                tx_delta = net[iface].bytes_sent - prev_net[iface].bytes_sent
                rx_total += rx_delta
                tx_total += tx_delta
            prev_net = net

            line = (
                f"[{now}] CPU {cpu:5.1f}% | Mem {mem.percent:5.1f}% "
                f"(used {mem.used / (1024**3):.2f} GB/{mem.total / (1024**3):.2f} GB) | "
                f"Net ↓ {fmt_bytes_per_s(rx_total / args.interval)} ↑ {fmt_bytes_per_s(tx_total / args.interval)}"
            )

            if tracked_proc:
                try:
                    proc_cpu = tracked_proc.cpu_percent(interval=None) / psutil.cpu_count()
                    proc_mem = tracked_proc.memory_info().rss / (1024**2)
                    line += f" | {tracked_proc.name()} cpu {proc_cpu:5.1f}% mem {proc_mem:.0f} MB"
                except psutil.NoSuchProcess:
                    line += " | process ended"
                    tracked_proc = None

            print(line)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
