#!/usr/bin/env python3
import os

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".mplconfig"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch


def box(ax, xy, text, width=1.9, height=0.6):
    x, y = xy
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.02,rounding_size=0.05",
        linewidth=1.2,
        edgecolor="#2d2d2d",
        facecolor="#f6f6f6",
    )
    ax.add_patch(patch)
    ax.text(x + width / 2, y + height / 2, text, ha="center", va="center", fontsize=10)
    return patch


def arrow(ax, start, end):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="->",
            mutation_scale=12,
            linewidth=1.2,
            color="#2d2d2d",
        )
    )


def main():
    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis("off")

    cam = box(ax, (0.5, 4.3), "Camera / Encoder")
    ingest = box(ax, (3.0, 4.3), "WebRTC Ingest\n(WHIP)")
    sfu = box(ax, (5.6, 4.3), "SFU")
    view1 = box(ax, (7.9, 5.2), "Viewer A")
    view2 = box(ax, (7.9, 4.3), "Viewer B")
    view3 = box(ax, (7.9, 3.4), "Viewer C")

    arrow(ax, (cam.get_x() + cam.get_width(), 4.6), (ingest.get_x(), 4.6))
    arrow(ax, (ingest.get_x() + ingest.get_width(), 4.6), (sfu.get_x(), 4.6))
    arrow(ax, (sfu.get_x() + sfu.get_width(), 4.9), (view1.get_x(), 5.45))
    arrow(ax, (sfu.get_x() + sfu.get_width(), 4.6), (view2.get_x(), 4.6))
    arrow(ax, (sfu.get_x() + sfu.get_width(), 4.3), (view3.get_x(), 3.75))

    out_dir = "plots/diagrams"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "webrtc_sfu_llhls.png")
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"Wrote diagram to {out_path}")


if __name__ == "__main__":
    main()
