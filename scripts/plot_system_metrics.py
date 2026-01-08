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
    ("avg_cpu_pct", "MacBook Average CPU (%)"),
    ("avg_mem_pct", "MacBook Average Memory (%)"),
]


def load_csv(path: str, label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["protocol"] = label
    return df


def plot_metric(df: pd.DataFrame, metric: str, label: str, output_dir: str) -> None:
    plt.figure(figsize=(8, 4))
    sns.lineplot(data=df, x="num_clients", y=metric, hue="protocol", marker="o")
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
        description="Plot Mac system metrics (CPU/memory) for WebRTC vs LL-HLS."
    )
    parser.add_argument(
        "--webrtc",
        default="webrtc/client/qoe-results/system_metrics.csv",
        help="Path to WebRTC system_metrics.csv",
    )
    parser.add_argument(
        "--llhls",
        default="ll-hls/client/qoe-results/system_metrics.csv",
        help="Path to LL-HLS system_metrics.csv",
    )
    parser.add_argument(
        "--out",
        default="plots/system_metrics",
        help="Output directory for plots",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    df_webrtc = load_csv(args.webrtc, "WebRTC")
    df_llhls = load_csv(args.llhls, "LL-HLS")
    df = pd.concat([df_webrtc, df_llhls], ignore_index=True)

    df["num_clients"] = pd.to_numeric(df["num_clients"], errors="coerce")
    df = df.dropna(subset=["num_clients"])
    df = df.sort_values(["num_clients", "protocol"])

    for metric, label in METRICS:
        if metric not in df.columns:
            print(f"Skipping {metric}: column not found")
            continue
        plot_metric(df, metric, label, args.out)

    print(f"Wrote plots to {args.out}")


if __name__ == "__main__":
    main()
