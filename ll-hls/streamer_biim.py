#!/usr/bin/env python3
"""
True Low-Latency HLS (LL-HLS) streamer using biim + FFmpeg.

This script uses FFmpeg for video capture/encoding and biim for LL-HLS packaging.
biim provides full Apple LL-HLS support including:
- EXT-X-PART (partial segments)
- Blocking playlist reload
- EXT-X-PRELOAD-HINT
- In-memory serving (no disk I/O)

Example:
    python3 streamer_biim.py --device /dev/video0 --http-port 8080
    
    # Or with test source:
    python3 streamer_biim.py --test-src --http-port 8080

Requires:
    - FFmpeg with libx264 support
    - biim library (pip install biim or clone from https://github.com/monyone/biim)
    - aiohttp
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any

from aiohttp import web

# Add biim to path if installed locally
BIIM_PATH = Path(__file__).parent / "biim"
if BIIM_PATH.exists():
    sys.path.insert(0, str(BIIM_PATH))

try:
    from biim.mpeg2ts import ts
    from biim.mpeg2ts.pat import PATSection
    from biim.mpeg2ts.pmt import PMTSection
    from biim.mpeg2ts.pes import PES
    from biim.mpeg2ts.h264 import H264PES
    from biim.mpeg2ts.h265 import H265PES
    from biim.mpeg2ts.parser import SectionParser, PESParser
    from biim.variant.fmp4 import Fmp4VariantHandler
    from biim.util.reader import BufferingAsyncReader
    BIIM_AVAILABLE = True
except ImportError as e:
    BIIM_AVAILABLE = False
    print(f"biim library not found: {e}")
    print("Install with: pip install biim")
    print("Or clone from: https://github.com/monyone/biim")


LOG = logging.getLogger("ll-hls-biim")

# Global timestamp storage for segment production times
_PART_TIMESTAMPS: Dict[str, int] = {}
_SEGMENT_TIMESTAMPS: Dict[str, int] = {}
_CURRENT_SEGMENT_INDEX = 0
_CURRENT_PART_INDEX = 0


def _record_new_segments_from_m3u8(m3u8_obj: Any) -> None:
    """Update the timestamp map using the absolute media sequence, not window size."""
    global _CURRENT_SEGMENT_INDEX, _SEGMENT_TIMESTAMPS
    if not hasattr(m3u8_obj, "segments"):
        return

    try:
        # biim's m3u8 object exposes either `sequence` or `media_sequence`
        media_sequence = int(getattr(m3u8_obj, "sequence", getattr(m3u8_obj, "media_sequence", 0)))
    except Exception:
        media_sequence = 0

    segments = getattr(m3u8_obj, "segments", [])
    if not segments:
        return

    # media_sequence is 0-indexed (used as msn); filename is msn+1
    latest_file_index = media_sequence + len(segments)
    if latest_file_index <= _CURRENT_SEGMENT_INDEX:
        return

    now = time.time_ns()
    for file_index in range(_CURRENT_SEGMENT_INDEX + 1, latest_file_index + 1):
        segment_name = f"segment{file_index:05d}.m4s"
        _SEGMENT_TIMESTAMPS[segment_name] = now
        LOG.debug(
            f"Recorded timestamp for {segment_name} "
            f"(media_sequence={media_sequence}, window={len(segments)})"
        )

        # Keep a rolling window of recent segments (enough for benchmarking)
        if len(_SEGMENT_TIMESTAMPS) > 120:
            oldest = sorted(_SEGMENT_TIMESTAMPS.keys())[0]
            del _SEGMENT_TIMESTAMPS[oldest]

    _CURRENT_SEGMENT_INDEX = latest_file_index


class TimestampTrackingHandler:
    """Wrapper around Fmp4VariantHandler that tracks timestamps for latency measurement."""
    
    def __init__(self, handler: Any):
        self._handler = handler
        self._segment_index = 0
        self._part_index = 0
    
    def __getattr__(self, name):
        return getattr(self._handler, name)
    
    def h264(self, pes):
        """Track H.264 frame and record timestamp when a new part/segment starts."""
        global _PART_TIMESTAMPS, _SEGMENT_TIMESTAMPS, _CURRENT_PART_INDEX, _CURRENT_SEGMENT_INDEX
        
        # Get the current part/segment count before processing
        old_part_count = len(self._handler.m3u8.ongoing_parts) if hasattr(self._handler, 'm3u8') else 0
        
        # Process the frame
        result = self._handler.h264(pes)
        
        # Check if a new part was created and update segment timestamps using media sequence
        if hasattr(self._handler, 'm3u8'):
            new_part_count = len(self._handler.m3u8.ongoing_parts)
            
            if new_part_count != old_part_count:
                _CURRENT_PART_INDEX += 1
                part_name = f"part{_CURRENT_PART_INDEX:05d}"
                _PART_TIMESTAMPS[part_name] = time.time_ns()
                LOG.debug(f"New part: {part_name}")

            _record_new_segments_from_m3u8(self._handler.m3u8)

        return result
    
    def h265(self, pes):
        """Track H.265 frame and record timestamp."""
        global _SEGMENT_TIMESTAMPS, _CURRENT_SEGMENT_INDEX
        
        result = self._handler.h265(pes)
        
        if hasattr(self._handler, 'm3u8'):
            _record_new_segments_from_m3u8(self._handler.m3u8)
        
        return result


def build_ffmpeg_command(args: argparse.Namespace) -> list[str]:
    """Build FFmpeg command for video capture and encoding."""
    width, height = args.resolution
    
    if args.test_src:
        # Use FFmpeg test source with synthetic audio
        input_args = [
            "-f", "lavfi",
            "-i", f"testsrc=size={width}x{height}:rate={args.framerate}",
            "-f", "lavfi", 
            "-i", "sine=frequency=1000:sample_rate=48000",
        ]
        has_audio = True
    else:
        # Use V4L2 device (Linux webcam) - video only, generate silent audio
        input_args = [
            "-f", "v4l2",
            "-video_size", f"{width}x{height}",
            "-framerate", str(args.framerate),
            "-i", args.device,
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
        ]
        has_audio = True  # We generate silent audio
    
    # Add pixel format conversion and timestamp overlay
    # format=yuv420p is required for baseline profile compatibility
    vf_filters = [
        "format=yuv420p",
        f"drawtext=fontsize=24:fontcolor=red:x=10:y=10:text='%{{localtime}}'"
    ]
    
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        *input_args,
        "-vf", ",".join(vf_filters),
        "-c:v", "libx264",
        "-preset", args.speed_preset,
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-pix_fmt", "yuv420p",
        "-b:v", f"{args.bitrate}k",
        "-g", str(args.key_int_max),
        "-keyint_min", str(args.key_int_max),
        "-sc_threshold", "0",
        "-bf", "0",
        "-shortest",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mpegts",
        "-",
    ]
    
    return cmd


async def handle_timestamps(request: web.Request) -> web.Response:
    """Serve segment timestamps for latency measurement."""
    data = {
        "segments": _SEGMENT_TIMESTAMPS,
        "parts": _PART_TIMESTAMPS,
        "timestamp": time.time_ns()
    }
    return web.Response(
        text=json.dumps(data),
        content_type="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
        }
    )


async def run_biim_pipeline(args: argparse.Namespace) -> None:
    """Run the biim LL-HLS pipeline with FFmpeg input."""
    
    if not BIIM_AVAILABLE:
        LOG.error("biim library is not available")
        return
    
    loop = asyncio.get_running_loop()
    
    # Create biim handler
    base_handler = Fmp4VariantHandler(
        target_duration=args.target_duration,
        part_target=args.part_duration,
        window_size=args.window_size,
        has_video=True,
        has_audio=True,  # Always have audio (real or silent)
    )
    
    # Wrap with timestamp tracking
    handler = base_handler  # Use base handler directly, timestamps tracked separately
    
    # Setup aiohttp with biim routes + our timestamp endpoint
    app = web.Application()
    
    # Serve the client HTML
    async def serve_client(request: web.Request) -> web.Response:
        client_path = Path(__file__).parent / "client" / "browser.html"
        if client_path.exists():
            return web.Response(
                text=client_path.read_text(),
                content_type="text/html",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        return web.Response(text="Client not found", status=404)
    
    app.add_routes([
        web.get('/', serve_client),  # Serve client at root
        web.get('/client', serve_client),  # Alias
        web.get('/playlist.m3u8', handler.playlist),
        web.get('/live.m3u8', handler.playlist),  # Alias for compatibility
        web.get('/segment', handler.segment),
        web.get('/part', handler.partial),
        web.get('/init', handler.initialization),
        web.get('/timestamps.json', handle_timestamps),
    ])
    
    # Add CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response
    
    app.middlewares.append(cors_middleware)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.http_host, args.http_port)
    await site.start()
    
    LOG.info(f"LL-HLS server running at http://{args.http_host}:{args.http_port}")
    LOG.info(f"Playlist URL: http://{args.http_host}:{args.http_port}/playlist.m3u8")
    LOG.info(f"Timestamps URL: http://{args.http_host}:{args.http_port}/timestamps.json")
    
    # Start FFmpeg process
    ffmpeg_cmd = build_ffmpeg_command(args)
    LOG.info(f"Starting FFmpeg: {' '.join(ffmpeg_cmd)}")
    
    ffmpeg_proc = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    # Background task to log FFmpeg stderr
    async def log_ffmpeg_stderr():
        while True:
            line = await ffmpeg_proc.stderr.readline()
            if not line:
                break
            LOG.info(f"FFmpeg: {line.decode().strip()}")
    
    stderr_task = asyncio.create_task(log_ffmpeg_stderr())
    
    # Setup MPEG-TS parsers
    PAT_Parser: SectionParser[PATSection] = SectionParser(PATSection)
    PMT_Parser: SectionParser[PMTSection] = SectionParser(PMTSection)
    AAC_PES_Parser: PESParser[PES] = PESParser(PES)
    H264_PES_Parser: PESParser[H264PES] = PESParser(H264PES)
    H265_PES_Parser: PESParser[H265PES] = PESParser(H265PES)
    
    PMT_PID: Optional[int] = None
    AAC_PID: Optional[int] = None
    H264_PID: Optional[int] = None
    H265_PID: Optional[int] = None
    PCR_PID: Optional[int] = None
    
    reader = ffmpeg_proc.stdout
    
    LOG.info("Starting MPEG-TS demuxing...")
    
    try:
        while True:
            # Read sync byte
            sync_byte = await reader.read(1)
            if sync_byte == b'':
                LOG.info("FFmpeg stream ended")
                break
            if sync_byte != ts.SYNC_BYTE:
                continue
            
            # Read rest of packet
            try:
                packet = ts.SYNC_BYTE + await reader.readexactly(ts.PACKET_SIZE - 1)
            except asyncio.IncompleteReadError:
                break
            
            PID = ts.pid(packet)
            
            # Extract PCR from any packet that has it (including video packets)
            if ts.has_pcr(packet):
                pcr_value = ts.pcr(packet)
                if not hasattr(handler, '_pcr_logged'):
                    handler._pcr_logged = True
                    LOG.info(f"First PCR received: {pcr_value}")
                handler.pcr(pcr_value)
            
            # Parse video
            if PID == H264_PID:
                H264_PES_Parser.push(packet)
                for H264 in H264_PES_Parser:
                    # Record timestamp when processing frame
                    handler.h264(H264)
                    
                    # Log first few frames and init status
                    if not hasattr(handler, '_frame_count'):
                        handler._frame_count = 0
                    handler._frame_count += 1
                    if handler._frame_count <= 5 or handler._frame_count % 100 == 0:
                        init_done = handler.init.done() if handler.init else "N/A"
                        has_video_track = handler.video_track is not None
                        has_audio_track = handler.audio_track is not None
                        LOG.info(f"H264 frame {handler._frame_count}: init={init_done}, video_track={has_video_track}, audio_track={has_audio_track}")
                    
                    # Track segment creation for timestamps
                    global _CURRENT_SEGMENT_INDEX, _SEGMENT_TIMESTAMPS
                    if hasattr(handler, 'm3u8'):
                        _record_new_segments_from_m3u8(handler.m3u8)
            
            elif PID == H265_PID:
                H265_PES_Parser.push(packet)
                for H265 in H265_PES_Parser:
                    handler.h265(H265)
            
            elif PID == AAC_PID:
                AAC_PES_Parser.push(packet)
                for AAC in AAC_PES_Parser:
                    handler.aac(AAC)
            
            elif PID == 0x00:
                # PAT
                PAT_Parser.push(packet)
                for PAT in PAT_Parser:
                    if PAT.CRC32() != 0:
                        continue
                    for program_number, program_map_PID in PAT:
                        if program_number == 0:
                            continue
                        if not PMT_PID:
                            PMT_PID = program_map_PID
                            LOG.info(f"Found PMT PID: {PMT_PID}")
            
            elif PID == PMT_PID:
                # PMT
                PMT_Parser.push(packet)
                for PMT in PMT_Parser:
                    if PMT.CRC32() != 0:
                        continue
                    PCR_PID = PMT.PCR_PID
                    for stream_type, elementary_PID, _ in PMT:
                        if stream_type == 0x1b:  # H.264
                            if not H264_PID:
                                LOG.info(f"Found H.264 PID: {elementary_PID}")
                            H264_PID = elementary_PID
                        elif stream_type == 0x24:  # H.265
                            if not H265_PID:
                                LOG.info(f"Found H.265 PID: {elementary_PID}")
                            H265_PID = elementary_PID
                        elif stream_type == 0x0F:  # AAC
                            if not AAC_PID:
                                LOG.info(f"Found AAC PID: {elementary_PID}")
                            AAC_PID = elementary_PID
    
    except asyncio.CancelledError:
        LOG.info("Pipeline cancelled")
    finally:
        ffmpeg_proc.terminate()
        await ffmpeg_proc.wait()
        await runner.cleanup()


def build_arg_parser() -> argparse.ArgumentParser:
    """Build argument parser for the biim-based streamer."""
    parser = argparse.ArgumentParser(
        description="True LL-HLS streamer using biim + FFmpeg"
    )
    parser.add_argument(
        "--device", default="/dev/video0",
        help="Video4Linux device to capture from"
    )
    parser.add_argument(
        "--test-src", action="store_true",
        help="Use FFmpeg test source instead of webcam"
    )
    parser.add_argument(
        "--resolution", default="1280x720",
        type=lambda x: tuple(map(int, x.lower().split('x'))),
        help="Frame size in WIDTHxHEIGHT (default: 1280x720)"
    )
    parser.add_argument(
        "--framerate", default=30, type=int,
        help="Capture frame rate (FPS)"
    )
    parser.add_argument(
        "--bitrate", default=2500, type=int,
        help="Target video bitrate in kbit/s (default: 2500)"
    )
    parser.add_argument(
        "--key-int-max", default=30, type=int,
        help="Maximum GOP size/keyframe interval (default: 30)"
    )
    parser.add_argument(
        "--target-duration", default=1, type=int,
        help="Target segment duration in seconds (default: 1)"
    )
    parser.add_argument(
        "--part-duration", default=0.1, type=float,
        help="LL-HLS part duration in seconds (default: 0.1)"
    )
    parser.add_argument(
        "--window-size", default=5, type=int,
        help="Number of segments in live window (default: 5)"
    )
    parser.add_argument(
        "--http-host", default="0.0.0.0",
        help="HTTP server bind address"
    )
    parser.add_argument(
        "--http-port", default=8080, type=int,
        help="HTTP server port"
    )
    parser.add_argument(
        "--speed-preset", default="ultrafast",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
        help="x264 speed preset (default: ultrafast)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level"
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    if not BIIM_AVAILABLE:
        LOG.error("biim library not available. Please install it:")
        LOG.error("  pip install biim")
        LOG.error("  or: git clone https://github.com/monyone/biim.git")
        sys.exit(1)
    
    LOG.info("Starting True LL-HLS streamer with biim")
    LOG.info(f"Target duration: {args.target_duration}s, Part duration: {args.part_duration}s")
    
    try:
        asyncio.run(run_biim_pipeline(args))
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")


if __name__ == "__main__":
    main()
