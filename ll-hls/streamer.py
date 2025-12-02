#!/usr/bin/env python3
"""
Low-latency HLS streaming helper built on top of GStreamer.

The script captures a live video source (webcam by default), encodes it with
settings suitable for low-latency delivery, produces CMAF/HLS segments via
``hlssink2`` and serves them over a lightweight HTTP server.

Example (webcam on /dev/video0, serve on port 8080):

    python3 streamer.py 
    or possibly with options:
    python3 streamer.py --device /dev/video0 --http-port 8080
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import threading
import json
import os
import re
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from inotify_simple import INotify, flags

from cli import build_arg_parser

try:
    import gi  # type: ignore

    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib, Gst  # type: ignore
except (ImportError, ValueError) as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "PyGObject with GStreamer bindings is required. "
        "Install it with 'sudo apt install python3-gi gstreamer1.0-tools "
        "gstreamer1.0-plugins-{base,good,bad,ugly}'"
    ) from exc


LOG = logging.getLogger("ll-hls")
_TIMESTAMP_FILE = None  
_SEGMENT_TIMESTAMPS = {} 
_OUTPUT_DIR = None   


def ensure_hlssink2_exists() -> None:
    factory = Gst.ElementFactory.find("hlssink2")
    if factory is None:
        raise SystemExit(
            "GStreamer element 'hlssink2' was not found. Install gst-plugins-bad "
            "(Ubuntu/Debian: sudo apt install gstreamer1.0-plugins-bad)"
        )


class QuietSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Simple handler that suppresses stdout logging noise."""

    def log_message(self, format: str, *args) -> None:  # pragma: no cover - delegate to logging module
        LOG.debug("HTTP %s", format % args)

    def end_headers(self) -> None:  # pragma: no cover - simple header hook
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # pragma: no cover - preflight handler
        self.send_response(204)
        self.end_headers()
    
    def do_GET(self) -> None:
        if self.path == "/timestamps.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            data = {"segments": _SEGMENT_TIMESTAMPS, "timestamp": time.time_ns()}
            self.wfile.write(json.dumps(data).encode())
        else:
            super().do_GET()


class ThreadedFileServer:
    def __init__(self, directory: Path, host: str, port: int) -> None:
        handler = partial(QuietSimpleHTTPRequestHandler, directory=str(directory))
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        LOG.info("Serving %s over HTTP on http://%s:%s", directory, host, port)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2)


def build_pipeline_description(args: argparse.Namespace) -> str:
    width, height = args.resolution
    caps = f"video/x-raw,width={width},height={height},framerate={args.framerate}/1"
    source = "videotestsrc is-live=true" if args.test_src else f"v4l2src device={args.device}"

    pipeline = (
        f"{source} ! queue leaky=downstream max-size-buffers=1 ! "
        f"videoconvert ! timeoverlay color=0xffff0000 valignment=top halignment=left ! "
        f"videoscale ! videorate ! {caps} ! "
        f"x264enc tune=zerolatency speed-preset={args.speed_preset} bitrate={args.bitrate} "
        f"key-int-max={args.key_int_max} sliced-threads=true bframes=0 byte-stream=true aud=true ! "
        f"h264parse config-interval=-1 name=videoparse"
    )

    LOG.debug("Pipeline description: %s", pipeline)
    return pipeline

def monitor_segments():
    """Monitor the output directory for new segments and record their timestamps."""
    global _SEGMENT_TIMESTAMPS, _OUTPUT_DIR
    if not _OUTPUT_DIR:
        return
    
    try:
        inotify = INotify()
        watch_flags = flags.CREATE | flags.CLOSE_WRITE
        wd = inotify.add_watch(str(_OUTPUT_DIR), watch_flags)
        LOG.info("Started inotify watch on %s for segment monitoring", _OUTPUT_DIR)
        while True:
            for event in inotify.read(timeout=1000):
                if event.name and event.name.endswith('.ts'):
                    if event.mask & flags.CLOSE_WRITE:
                        match = re.search(r'segment_(\d+)\.ts', event.name)
                        if match:
                            segment_num = int(match.group(1))
                            timestamp_ns = time.time_ns()
                            _SEGMENT_TIMESTAMPS[segment_num] = timestamp_ns
                            LOG.info(f"Recorded timestamp for segment {segment_num}: {timestamp_ns}")

                            if len(_SEGMENT_TIMESTAMPS) > 20:
                                min_key = min(_SEGMENT_TIMESTAMPS.keys())
                                del _SEGMENT_TIMESTAMPS[min_key]

    except ImportError:
        LOG.error("inotify_simple module is required for segment monitoring. Please install it via pip.")
    except Exception as e:
        LOG.error(f"Error in segment monitoring thread: {e}")                            


def set_property_if_available(element: Gst.Element, name: str, value) -> None:
    if element.find_property(name) is None:
        LOG.debug("Skipping property '%s' on %s (not supported by this GStreamer build)", name, element.name)
        return
    element.set_property(name, value)


def configure_hlssink(hlssink: Gst.Element, playlist_path: Path, segment_path: Path, args: argparse.Namespace, playlist_root: str) -> None:
    props = {
        "playlist-location": str(playlist_path),
        "location": str(segment_path),
        "playlist-length": args.playlist_length,
        "max-files": args.max_files,
        "target-duration": max(1, int(round(args.target_duration))),
        "segment-duration": args.segment_duration,
        "part-duration": args.segment_duration / 3,
        "playlist-root": playlist_root,
        "send-keyframe-requests": True,
    }
    for name, value in props.items():
        set_property_if_available(hlssink, name, value)


def create_pipeline(args: argparse.Namespace, playlist_path: Path, segment_path: Path, playlist_root: str) -> Gst.Pipeline:
    pipeline_desc = build_pipeline_description(args)
    pipeline = Gst.parse_launch(pipeline_desc)

    hlssink = Gst.ElementFactory.make("hlssink2", "hlssink")
    if hlssink is None:
        raise SystemExit("Failed to instantiate hlssink2 element")

    pipeline.add(hlssink)
    configure_hlssink(hlssink, playlist_path, segment_path, args, playlist_root)

    parser = pipeline.get_by_name("videoparse")
    if parser is None:
        raise RuntimeError("h264parse element 'videoparse' not found in pipeline")

    parser_src = parser.get_static_pad("src")
    if parser_src is None:
        raise RuntimeError("h264parse 'videoparse' does not expose a src pad")

    sink_pad = hlssink.request_pad_simple("video")
    if sink_pad is None:
        raise RuntimeError("hlssink2 could not provide a 'video' sink pad")

    if parser_src.link(sink_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError("Failed to link h264parse to hlssink")

    return pipeline


def run_pipeline(pipeline: Gst.Pipeline) -> None:
    loop = GLib.MainLoop()

    def _on_message(bus: Gst.Bus, message: Gst.Message) -> None:
        msg_type = message.type
        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            LOG.error("GStreamer error: %s (debug: %s)", err, debug)
            loop.quit()
        elif msg_type in (Gst.MessageType.EOS, Gst.MessageType.WARNING):
            if msg_type == Gst.MessageType.WARNING:
                warn, debug = message.parse_warning()
                LOG.warning("GStreamer warning: %s (debug: %s)", warn, debug)
            else:
                LOG.info("Received EOS from pipeline, stopping loop")
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _on_message)

    def _handle_signal(signum, _frame) -> None:  # pragma: no cover - signal hook
        LOG.info("Signal %s received, shutting down pipeline", signum)
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    LOG.info("Setting pipeline to PLAYING")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        LOG.info("Stopping pipeline")
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()


def main() -> None:
    global _OUTPUT_DIR
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s [%(levelname)s] %(message)s")

    Gst.init(None)
    ensure_hlssink2_exists()

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    
    for old_segment in output_dir.glob("segment_*.ts"):
        old_segment.unlink()
        LOG.debug(f"Removed old segment: {old_segment.name}")
    
    old_playlist = output_dir / args.playlist_name
    if old_playlist.exists():
        old_playlist.unlink()
        LOG.debug(f"Removed old playlist: {old_playlist.name}")
    
    _OUTPUT_DIR = output_dir

    playlist_path = output_dir / args.playlist_name
    segment_path = output_dir / f"{args.segment_prefix}_%05d.ts"
    playlist_root = args.public_url or f"http://{args.http_host if args.http_host != '0.0.0.0' else '127.0.0.1'}:{args.http_port}"

    pipeline = create_pipeline(args, playlist_path, segment_path, playlist_root)
    file_server = ThreadedFileServer(output_dir, args.http_host, args.http_port)
    file_server.start()
    
    LOG.info("=" * 60)
    LOG.info("HLS stream is now available:")
    LOG.info(f"Local: http://127.0.0.1:{args.http_port}/live.m3u8")
    
    try:
        hostname = socket.gethostname()
        local_ips = []
        for ip in socket.getaddrinfo(hostname, None):
            addr = ip[4][0]
            if ':' not in addr and addr != '127.0.0.1':  
                if addr not in local_ips:
                    local_ips.append(addr)
        
        if local_ips:
            LOG.info("Network:")
            for ip in local_ips:
                LOG.info(f"http://{ip}:{args.http_port}/live.m3u8")
    except Exception as e:
        LOG.debug(f"Could not determine network addresses: {e}")
    
    LOG.info("=" * 60)
    
    monitor_thread = threading.Thread(target=monitor_segments, daemon=True)
    monitor_thread.start()
    LOG.info("Started segment timestamp monitoring")

    try:
        run_pipeline(pipeline)
    finally:
        file_server.stop()


if __name__ == "__main__":
    main()
