import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Minimal WebRTC loopback using aiortc + aiohttp"
    )
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument("--play-from", help="Read the media from a file and sent it.")
    parser.add_argument(
        "--play-without-decoding",
        help=(
            "Read the media without decoding it (experimental). "
            "For now it only works with an MPEGTS container with only H.264 video."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    parser.add_argument(
        "--video-device",
        default="/dev/video0",
        help="Video device path on the server (v4l2). Set to 'none' to disable.",
    )
    parser.add_argument(
        "--video-format",
        default="v4l2",
        help="Capture format for the video device (default: v4l2).",
    )
    parser.add_argument(
        "--video-size",
        default="640x480",
        help="Frame size for the video device, e.g. 640x480.",
    )
    parser.add_argument(
        "--audio-codec", help="Force a specific audio codec (e.g. audio/opus)"
    )
    parser.add_argument(
        "--video-codec", help="Force a specific video codec (e.g. video/H264)"
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Expose a single entry point for the server to parse CLI args."""
    return build_parser().parse_args(argv)
