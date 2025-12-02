"""
Command-line argument utilities for the LL-HLS streamer helper.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x")
        return int(width), int(height)
    except ValueError as exc:  # pragma: no cover - CLI guard
        raise argparse.ArgumentTypeError(f"Invalid resolution '{value}'. Expected form WIDTHxHEIGHT") from exc


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:  # pragma: no cover - CLI guard
        raise argparse.ArgumentTypeError(f"{value} is not a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be positive")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be positive")
    return number


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Webcam to Low-Latency HLS streamer")
    parser.add_argument("--device", default="/dev/video0", help="Video4Linux device to capture from")
    parser.add_argument("--test-src", action="store_true", help="Use 'videotestsrc' instead of the webcam")
    parser.add_argument(
        "--resolution", default="1280x720", type=parse_resolution, help="Frame size in WIDTHxHEIGHT (default: 1280x720)"
    )
    parser.add_argument("--framerate", default=30, type=positive_int, help="Capture frame rate (FPS)")
    parser.add_argument(
        "--bitrate",
        default=2500,
        type=positive_int,
        help="Target video bitrate in kbit/s for the encoder (default: 2500)",
    )
    parser.add_argument(
        "--key-int-max",
        default=60,
        type=positive_int,
        help="Maximum GOP size/keyframe interval (default: 60 frames)",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        type=Path,
        help="Directory where HLS segments and playlist will be written",
    )
    parser.add_argument("--playlist-name", default="live.m3u8", help="Name of the HLS playlist file")
    parser.add_argument("--segment-prefix", default="segment", help="Prefix for generated HLS segments")
    parser.add_argument("--playlist-length", default=6, type=positive_int, help="Number of segments in the playlist")
    parser.add_argument("--max-files", default=20, type=positive_int, help="Maximum number of segments kept on disk")
    parser.add_argument(
        "--target-duration",
        default=1.0,
        type=positive_float,
        help="Advertised EXT-X-TARGETDURATION value (seconds)",
    )
    parser.add_argument(
        "--segment-duration",
        default=1.0,
        type=positive_float,
        help="Length of each segment in seconds (keep low for LL-HLS)",
    )
    parser.add_argument("--http-host", default="0.0.0.0", help="HTTP server bind address")
    parser.add_argument("--http-port", default=8080, type=positive_int, help="HTTP server port")
    parser.add_argument(
        "--public-url",
        default=None,
        help="Base URL clients will use (defaults to http://<host>:<port>; set explicitly when binding 0.0.0.0)",
    )
    parser.add_argument(
        "--speed-preset",
        default="ultrafast",
        choices=[
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        ],
        help="x264 speed preset",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Verbosity of the Python helper logs",
    )
    return parser
