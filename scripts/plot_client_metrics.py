#!/usr/bin/env python3
import argparse
import os

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".mplconfig"))

import pandas as pd
import seaborn as sns
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


METRICS = [
    "avg_ttff_ms",
    "avg_latency_ms",
    "p50_latency_ms",
    "p95_latency_ms",
]

STALL_METRICS = [
    "avg_stalls_per_client",
    "avg_stall_ms_per_client",
    "total_stalls",
    "total_stall_ms",
    "pct_clients_with_stall",
    "p50_stall_ms_per_client",
    "p95_stall_ms_per_client",
]

METRIC_LABELS = {
    "avg_ttff_ms": "Average TTFF (ms)",
    "avg_latency_ms": "Average Latency (ms)",
    "p50_latency_ms": "P50 Latency (ms)",
    "p95_latency_ms": "P95 Latency (ms)",
    "avg_stalls_per_client": "Average Stalls per Client",
    "avg_stall_ms_per_client": "Average Stall Duration per Client (ms)",
    "total_stalls": "Total Stalls",
    "total_stall_ms": "Total Stall Duration (ms)",
    "pct_clients_with_stall": "Clients With Stall (%)",
    "p50_stall_ms_per_client": "P50 Stall Duration per Client (ms)",
    "p95_stall_ms_per_client": "P95 Stall Duration per Client (ms)",
}


def load_csv(path: str, label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["protocol"] = label
    return df


def plot_metric(df: pd.DataFrame, metric: str, output_dir: str, smooth_window: int) -> None:
    plt.figure(figsize=(8, 4))
    y_col = metric
    if smooth_window and smooth_window > 1:
        df = df.sort_values(["protocol", "num_clients"]).copy()
        smooth_col = f"{metric}__smooth"
        df[smooth_col] = (
            df.groupby("protocol")[metric]
            .transform(lambda s: s.rolling(smooth_window, center=True, min_periods=1).mean())
        )
        y_col = smooth_col
    sns.lineplot(data=df, x="num_clients", y=y_col, hue="protocol", marker="o")
    label = METRIC_LABELS.get(metric, metric)
    plt.title(label)
    plt.xlabel("Number of Clients")
    plt.ylabel(label)
    plt.legend(title="Protocol")
    plt.tight_layout()
    out_path = os.path.join(output_dir, f"{metric}.png")
    plt.savefig(out_path, dpi=150)
    plt.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot client aggregate metrics comparison between WebRTC and LL-HLS."
    )
    parser.add_argument(
        "--webrtc",
        default="webrtc/client/qoe-results/aggregate_results.csv",
        help="Path to WebRTC aggregate_results.csv",
    )
    parser.add_argument(
        "--llhls",
        default="ll-hls/client/qoe-results/aggregate_results.csv",
        help="Path to LL-HLS aggregate_results.csv",
    )
    parser.add_argument(
        "--out",
        default="plots/client_metrics",
        help="Output directory for plots",
    )
    parser.add_argument(
        "--include-stalls",
        action="store_true",
        help="Include stall-related metrics when present",
    )
    parser.add_argument(
        "--smooth-window",
        type=int,
        default=1,
        help="Rolling window size (in points) for optional smoothing; 1 disables.",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    df_webrtc = load_csv(args.webrtc, "WebRTC")
    df_llhls = load_csv(args.llhls, "LL-HLS")
    df = pd.concat([df_webrtc, df_llhls], ignore_index=True)

    df["num_clients"] = pd.to_numeric(df["num_clients"], errors="coerce")
    df = df.dropna(subset=["num_clients"])
    df = df.sort_values(["num_clients", "protocol"])

    for metric in METRICS:
        if metric in df.columns:
            plot_metric(df, metric, args.out, args.smooth_window)

    if args.include_stalls:
        for metric in STALL_METRICS:
            if metric in df.columns:
                plot_metric(df, metric, args.out, args.smooth_window)

    print(f"Wrote plots to {args.out}")


if __name__ == "__main__":
    main()
