#!/usr/bin/env python3
import argparse
import os

# Ensure matplotlib cache dir is writable before importing it.
os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".mplconfig"))

import pandas as pd
import seaborn as sns
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


METRICS = [
    "avg_sys_cpu_pct",
    "avg_sys_mem_pct",
    "avg_proc_cpu_pct",
    "avg_proc_rss_mb",
    "avg_net_rx_kbps",
    "net_counter_out_bytes",
    "net_out_mbps",
    "net_out_bytes_per_client",
    "net_out_packets_per_client",
    "net_out_mbps_per_client",
]

METRIC_LABELS = {
    "avg_sys_cpu_pct": "Average System CPU (%)",
    "avg_sys_mem_pct": "Average System Memory (%)",
    "avg_proc_cpu_pct": "Average Process CPU (%)",
    "avg_proc_rss_mb": "Average Process RSS (MB)",
    "avg_net_rx_kbps": "Average Network RX (kbps)",
    "net_counter_out_bytes": "Total Egress Bytes",
    "net_out_mbps": "Average Egress (Mbps)",
    "net_out_bytes_per_client": "Egress Bytes per Client",
    "net_out_packets_per_client": "Egress Packets per Client",
    "net_out_mbps_per_client": "Egress Mbps per Client",
}


def load_csv(path: str, label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["protocol"] = label
    return df


def add_network_metrics(df: pd.DataFrame) -> pd.DataFrame:
    numeric_cols = [
        "connected_clients",
        "duration_sec",
        "net_counter_out_bytes",
        "net_counter_out_packets",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "net_counter_out_bytes" in df.columns and "duration_sec" in df.columns:
        duration = df["duration_sec"].where(df["duration_sec"] > 0)
        df["net_out_mbps"] = (df["net_counter_out_bytes"] * 8) / duration / 1e6

    if "net_counter_out_bytes" in df.columns and "connected_clients" in df.columns:
        clients = df["connected_clients"].where(df["connected_clients"] > 0)
        df["net_out_bytes_per_client"] = df["net_counter_out_bytes"] / clients
        if "duration_sec" in df.columns:
            duration = df["duration_sec"].where(df["duration_sec"] > 0)
            df["net_out_mbps_per_client"] = (
                (df["net_counter_out_bytes"] * 8) / duration / 1e6 / clients
            )

    if "net_counter_out_packets" in df.columns and "connected_clients" in df.columns:
        clients = df["connected_clients"].where(df["connected_clients"] > 0)
        df["net_out_packets_per_client"] = df["net_counter_out_packets"] / clients

    return df


def plot_metric(df: pd.DataFrame, metric: str, output_dir: str, x_col: str) -> None:
    plt.figure(figsize=(8, 4))
    sns.lineplot(data=df, x=x_col, y=metric, hue="protocol", marker="o")
    label = METRIC_LABELS.get(metric, metric)
    plt.title(label)
    plt.xlabel("Number of Clients")
    plt.ylabel(label)
    plt.legend(title="Protocol")
    plt.tight_layout()
    out_path = os.path.join(output_dir, f"{metric}.png")
    plt.savefig(out_path, dpi=150)
    plt.close()


def plot_metric_by_protocol(df: pd.DataFrame, metric: str, output_dir: str, x_col: str) -> None:
    protocols = [p for p in df["protocol"].dropna().unique()]
    if not protocols:
        return
    preferred = ["LL-HLS", "WebRTC"]
    ordered = [p for p in preferred if p in protocols]
    ordered += [p for p in protocols if p not in ordered]
    label = METRIC_LABELS.get(metric, metric)

    fig, axes = plt.subplots(1, len(ordered), figsize=(6 * len(ordered), 4))
    if len(ordered) == 1:
        axes = [axes]
    for ax, protocol in zip(axes, ordered):
        subset = df[df["protocol"] == protocol]
        color = "#f28e2b" if protocol == "WebRTC" else None
        sns.lineplot(
            data=subset,
            x=x_col,
            y=metric,
            marker="o",
            ax=ax,
            color=color,
        )
        ax.set_title(f"{label} - {protocol}")
        ax.set_xlabel("Number of Clients")
        ax.set_ylabel(label)

    plt.tight_layout()
    out_path = os.path.join(output_dir, f"{metric}_by_protocol.png")
    plt.savefig(out_path, dpi=150)
    plt.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot server metrics comparison between WebRTC and LL-HLS."
    )
    parser.add_argument(
        "--webrtc",
        default="webrtc/server_metrics.csv",
        help="Path to WebRTC server_metrics.csv",
    )
    parser.add_argument(
        "--llhls",
        default="ll-hls/server_metrics.csv",
        help="Path to LL-HLS server_metrics.csv",
    )
    parser.add_argument(
        "--out",
        default="plots/server_metrics",
        help="Output directory for plots",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    df_webrtc = load_csv(args.webrtc, "WebRTC")
    df_llhls = load_csv(args.llhls, "LL-HLS")
    df = pd.concat([df_webrtc, df_llhls], ignore_index=True)
    df = add_network_metrics(df)

    x_col = "expected_clients" if "expected_clients" in df.columns else "num_clients"
    if x_col not in df.columns:
        raise SystemExit("No client count column found (expected 'expected_clients' or 'num_clients').")

    df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
    df = df.dropna(subset=[x_col])
    df = df.sort_values([x_col, "protocol"])

    for metric in METRICS:
        if metric not in df.columns:
            continue
        plot_metric(df, metric, args.out, x_col)
        if metric == "avg_proc_rss_mb":
            plot_metric_by_protocol(df, metric, args.out, x_col)

    print(f"Wrote plots to {args.out}")


if __name__ == "__main__":
    main()
