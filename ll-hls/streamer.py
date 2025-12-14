#!/usr/bin/env python3
"""
Low-latency HLS streaming helper built on top of GStreamer.

The script captures a live video source (webcam by default), encodes it with
settings suitable for low-latency delivery, produces CMAF fragments via
hlscmafsink (preferred) or hlssink2 (fallback), and serves them with a 
lightweight HTTP server.

Example (webcam on /dev/video0, serve on port 8080):

    python3 streamer.py 
    or possibly with options:
    python3 streamer.py --device /dev/video0 --http-port 8080
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Set up GST_PLUGIN_PATH for custom plugins (hlscmafsink, cmafmux)
_CUSTOM_GST_PLUGIN_PATH = Path.home() / ".local" / "share" / "gstreamer-1.0" / "plugins"
if _CUSTOM_GST_PLUGIN_PATH.exists():
    existing_path = os.environ.get("GST_PLUGIN_PATH", "")
    if str(_CUSTOM_GST_PLUGIN_PATH) not in existing_path:
        os.environ["GST_PLUGIN_PATH"] = f"{_CUSTOM_GST_PLUGIN_PATH}:{existing_path}" if existing_path else str(_CUSTOM_GST_PLUGIN_PATH)

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
_SEGMENT_TIMESTAMPS = {}
_OUTPUT_DIR: Optional[Path] = None


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
        parsed = urlparse(self.path)
        if parsed.path == "/timestamps.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            data = {"segments": _SEGMENT_TIMESTAMPS, "timestamp": time.time_ns()}
            self.wfile.write(json.dumps(data).encode())
        elif parsed.path.endswith(".m3u8"):
            # Serve playlist with no-cache headers for live streaming
            super().do_GET()
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


class PlaylistManager:
    """Maintain LL-HLS playlist state and provide blocking reload support."""

    def __init__(
        self,
        playlist_path: Path,
        playlist_length: int,
        target_duration: float,
        part_target: float,
        hold_back: float,
        part_hold_back: float,
    ) -> None:
        self._playlist_path = playlist_path
        self._segments: Deque[dict] = deque()
        self._playlist_capacity = playlist_length
        self._next_sequence = 0
        self._base_sequence = 0
        self._version = 0
        self._target_duration = target_duration
        self._part_target = part_target
        self._hold_back = hold_back
        self._part_hold_back = part_hold_back
        self._condition = threading.Condition()
        self._playlist_bytes: Optional[bytes] = None
        self._preload_hint: Optional[str] = None
        self._init_written = False

    def ensure_init_segment(self, path: Path) -> None:
        """Extract init segment (ftyp+moov) from the first fragment."""
        if self._init_written:
            return
        init_path = self._playlist_path.parent / "init.mp4"
        try:
            # Read the fragment and extract ftyp + moov boxes for init segment
            with open(path, "rb") as f:
                data = f.read()
            
            # Parse MP4 boxes to find ftyp and moov
            init_data = self._extract_init_boxes(data)
            if init_data:
                with open(init_path, "wb") as f:
                    f.write(init_data)
                self._init_written = True
                LOG.info("Wrote LL-HLS init segment to %s (%d bytes)", init_path, len(init_data))
            else:
                # Fallback: copy the whole first segment (may work for some players)
                shutil.copyfile(path, init_path)
                self._init_written = True
                LOG.warning("Could not extract init boxes, copied full segment as init")
        except OSError as exc:
            LOG.error("Failed to write init segment: %s", exc)
    
    def _extract_init_boxes(self, data: bytes) -> Optional[bytes]:
        """Extract ftyp and moov boxes from MP4 data."""
        result = b""
        offset = 0
        found_ftyp = False
        found_moov = False
        
        while offset < len(data) - 8:
            # Read box size (4 bytes) and type (4 bytes)
            box_size = int.from_bytes(data[offset:offset+4], 'big')
            box_type = data[offset+4:offset+8]
            
            if box_size == 0:
                # Box extends to end of file
                break
            if box_size < 8:
                # Invalid box size
                break
            
            if box_type == b'ftyp':
                result += data[offset:offset+box_size]
                found_ftyp = True
                LOG.debug("Found ftyp box at offset %d, size %d", offset, box_size)
            elif box_type == b'moov':
                result += data[offset:offset+box_size]
                found_moov = True
                LOG.debug("Found moov box at offset %d, size %d", offset, box_size)
            
            offset += box_size
            
            # Stop once we have both required boxes
            if found_ftyp and found_moov:
                break
        
        if found_ftyp and found_moov:
            return result
        elif found_ftyp or found_moov:
            LOG.warning("Only found %s box, init segment may be incomplete", 
                       "ftyp" if found_ftyp else "moov")
            return result if result else None
        return None

    def add_segment(self, name: str, duration: float, program_time: str, preload_hint: str) -> None:
        with self._condition:
            sequence = self._next_sequence
            self._next_sequence += 1
            self._segments.append(
                {
                    "sequence": sequence,
                    "name": name,
                    "duration": duration,
                    "program_time": program_time,
                }
            )
            while len(self._segments) > self._playlist_capacity:
                removed = self._segments.popleft()
                self._base_sequence = removed["sequence"] + 1
            if self._segments:
                self._base_sequence = self._segments[0]["sequence"]
            self._preload_hint = preload_hint
            self._version += 1
            rendered = self._render_playlist_locked()
            if rendered:
                self._playlist_bytes = rendered
                self._write_playlist_to_disk_locked()
                self._condition.notify_all()

    def _render_playlist_locked(self) -> bytes:
        if not self._segments or not self._init_written:
            return b""
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:9",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(self._target_duration))}",
            f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,HOLD-BACK={self._hold_back:.3f},PART-HOLD-BACK={self._part_hold_back:.3f}",
            f"#EXT-X-PART-INF:PART-TARGET={self._part_target:.3f}",
            '#EXT-X-MAP:URI="init.mp4"',
            f"#EXT-X-MEDIA-SEQUENCE:{self._base_sequence}",
        ]
        for segment in self._segments:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{segment['program_time']}")
            lines.append(f"#EXT-X-PART:DURATION={segment['duration']:.3f},URI=\"{segment['name']}\"")
            lines.append(f"#EXTINF:{segment['duration']:.3f},")
            lines.append(segment["name"])
        if self._preload_hint:
            lines.append(f"#EXT-X-PRELOAD-HINT:TYPE=PART,URI=\"{self._preload_hint}\"")
        playlist_body = "\n".join(lines) + "\n"
        return playlist_body.encode("utf-8")

    def _write_playlist_to_disk_locked(self) -> None:
        if self._playlist_bytes is None:
            return
        tmp_path = self._playlist_path.with_suffix(".tmp")
        tmp_path.write_bytes(self._playlist_bytes)
        tmp_path.replace(self._playlist_path)

    def get_playlist(self, since_version: Optional[int], wait_seconds: float) -> Optional[Tuple[bytes, int]]:
        with self._condition:
            if self._playlist_bytes and since_version is None:
                return self._playlist_bytes, self._version
            if since_version is not None and self._playlist_bytes and since_version < self._version:
                return self._playlist_bytes, self._version
            if wait_seconds > 0:
                self._condition.wait(timeout=wait_seconds)
            if not self._playlist_bytes:
                return None
            return self._playlist_bytes, self._version


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

def monitor_segments(output_dir: Path, approx_duration: float, playlist_manager: Optional[PlaylistManager]) -> None:
    """Monitor the output directory for new HLS segments and record timestamps."""
    global _SEGMENT_TIMESTAMPS
    try:
        inotify = INotify()
        watch_flags = flags.CREATE | flags.CLOSE_WRITE
        inotify.add_watch(str(output_dir), watch_flags)
        LOG.info("Started inotify watch on %s for segment monitoring", output_dir)
        while True:
            for event in inotify.read(timeout=1000):
                if not event.name:
                    continue
                # Handle both .ts and .m4s segments
                if not (event.name.endswith(".ts") or event.name.endswith(".m4s")):
                    continue
                if not (event.mask & flags.CLOSE_WRITE):
                    continue
                
                # Extract segment number from filename
                match = re.match(r"(.*)(\d{5})\.(ts|m4s)$", event.name)
                if not match:
                    continue
                
                timestamp_ns = time.time_ns()
                
                # Use segment filename as key
                _SEGMENT_TIMESTAMPS[event.name] = timestamp_ns
                LOG.debug("Recorded timestamp for segment %s: %s", event.name, timestamp_ns)
                
                # Keep only last 30 segments by timestamp value
                if len(_SEGMENT_TIMESTAMPS) > 30:
                    oldest_key = min(_SEGMENT_TIMESTAMPS, key=_SEGMENT_TIMESTAMPS.get)
                    del _SEGMENT_TIMESTAMPS[oldest_key]
    except ImportError:
        LOG.error("inotify_simple module is required for segment monitoring. Please install it via pip.")
    except Exception as exc:
        LOG.error("Error in segment monitoring thread: %s", exc)
        import traceback
        LOG.error(traceback.format_exc())


def create_pipeline(args: argparse.Namespace, segment_path: Path, chunk_duration: float) -> Gst.Pipeline:
    """Create and return the GStreamer pipeline for HLS streaming."""
    width, height = args.resolution
    caps = f"video/x-raw,width={width},height={height},framerate={args.framerate}/1"
    source = "videotestsrc is-live=true" if args.test_src else f"v4l2src device={args.device}"
    
    output_dir = args.output_dir
    playlist_location = str(output_dir / args.playlist_name)
    
    # Check if hlscmafsink (LL-HLS with CMAF) is available
    hlscmafsink = Gst.ElementFactory.find("hlscmafsink")
    
    if hlscmafsink:
        # Use hlscmafsink for proper LL-HLS with CMAF segments
        LOG.info("Using hlscmafsink for Low-Latency HLS with CMAF segments")
        segment_location = str(output_dir / f"{args.segment_prefix}%05d.m4s")
        init_location = str(output_dir / "init%05d.mp4")
        
        # hlscmafsink requires stream-format=avc for H.264
        pipeline_str = (
            f"{source} ! queue leaky=downstream max-size-buffers=1 ! "
            f"videoconvert ! timeoverlay color=0xffff0000 valignment=top halignment=left ! "
            f"videoscale ! videorate ! {caps} ! "
            f"x264enc tune=zerolatency speed-preset={args.speed_preset} bitrate={args.bitrate} "
            f"key-int-max={args.key_int_max} sliced-threads=true bframes=0 ! "
            f"h264parse ! video/x-h264,stream-format=avc,alignment=au ! "
            f"hlscmafsink name=hlssink "
            f"location={segment_location} "
            f"init-location={init_location} "
            f"playlist-location={playlist_location} "
            f"target-duration={max(1, int(chunk_duration))} "
            f"latency=500000000"  # 500ms latency for low latency
        )
    else:
        # Fall back to hlssink2 with MPEG-TS segments (regular HLS, not LL-HLS)
        LOG.warning("hlscmafsink not available, falling back to hlssink2 (regular HLS, not LL-HLS)")
        segment_location = str(output_dir / f"{args.segment_prefix}%05d.ts")
        
        pipeline_str = (
            f"{source} ! queue leaky=downstream max-size-buffers=1 ! "
            f"videoconvert ! timeoverlay color=0xffff0000 valignment=top halignment=left ! "
            f"videoscale ! videorate ! {caps} ! "
            f"x264enc tune=zerolatency speed-preset={args.speed_preset} bitrate={args.bitrate} "
            f"key-int-max={args.key_int_max} sliced-threads=true bframes=0 byte-stream=true aud=true ! "
            f"h264parse config-interval=-1 ! "
            f"hlssink2 name=hlssink "
            f"location={segment_location} "
            f"playlist-location={playlist_location} "
            f"target-duration={max(1, int(chunk_duration))} "
            f"max-files={args.max_files} "
            f"playlist-length={args.playlist_length}"
        )
    
    LOG.debug("Pipeline description: %s", pipeline_str)
    pipeline = Gst.parse_launch(pipeline_str)
    
    # Configure the internal cmafmux fragment-duration for sub-second segments
    if hlscmafsink:
        hlssink_elem = pipeline.get_by_name("hlssink")
        if hlssink_elem:
            # hlscmafsink implements GstChildProxy - access the internal 'muxer' (cmafmux)
            muxer = hlssink_elem.get_child_by_name("muxer")
            if muxer:
                # Convert segment duration from seconds to nanoseconds
                fragment_duration_ns = int(chunk_duration * 1_000_000_000)
                muxer.set_property("fragment-duration", fragment_duration_ns)
                LOG.info("Set cmafmux fragment-duration to %d ns (%.2f s)", 
                         fragment_duration_ns, chunk_duration)
            else:
                LOG.warning("Could not access internal muxer element in hlscmafsink")
        else:
            LOG.warning("Could not find hlssink element in pipeline")
    
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

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Clean up old segments (both .m4s and .ts)
    for old_segment in list(output_dir.glob("segment_*.m4s")) + list(output_dir.glob("segment*.ts")):
        old_segment.unlink()
        LOG.debug(f"Removed old segment: {old_segment.name}")
    
    old_playlist = output_dir / args.playlist_name
    if old_playlist.exists():
        old_playlist.unlink()
        LOG.debug(f"Removed old playlist: {old_playlist.name}")
    init_path = output_dir / "init.mp4"
    if init_path.exists():
        init_path.unlink()
        LOG.debug("Removed old init segment")
    
    _OUTPUT_DIR = output_dir

    segment_path = output_dir / f"{args.segment_prefix}%05d.ts"
    chunk_duration = args.segment_duration

    pipeline = create_pipeline(args, segment_path, chunk_duration)
    file_server = ThreadedFileServer(output_dir, args.http_host, args.http_port)
    file_server.start()
    
    LOG.info("=" * 60)
    LOG.info("HLS stream is now available:")
    LOG.info(f"Local: http://127.0.0.1:{args.http_port}/{args.playlist_name}")
    
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
                LOG.info(f"http://{ip}:{args.http_port}/{args.playlist_name}")
    except Exception as e:
        LOG.debug(f"Could not determine network addresses: {e}")
    
    LOG.info("=" * 60)
    
    # Start segment monitoring thread for timestamps (not playlist management)
    monitor_thread = threading.Thread(target=monitor_segments, args=(output_dir, chunk_duration, None), daemon=True)
    monitor_thread.start()
    LOG.info("Started segment timestamp monitoring")

    try:
        run_pipeline(pipeline)
    finally:
        file_server.stop()


if __name__ == "__main__":
    main()
