#!/usr/bin/env python3
"""LL-HLS streamer using biim + FFmpeg."""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any

from aiohttp import web

# Add local biim to path (repo root / biim contains the package)
BIIM_PATH = Path(__file__).resolve().parent.parent / "biim"
if BIIM_PATH.exists():
    sys.path.insert(0, str(BIIM_PATH))

from biim.mpeg2ts import ts
from biim.mpeg2ts.pat import PATSection
from biim.mpeg2ts.pmt import PMTSection
from biim.mpeg2ts.pes import PES
from biim.mpeg2ts.h264 import H264PES
from biim.mpeg2ts.parser import SectionParser, PESParser
from biim.variant.fmp4 import Fmp4VariantHandler

LOG = logging.getLogger("ll-hls")
_SEGMENT_TIMESTAMPS: Dict[str, int] = {}
_PART_TIMESTAMPS: Dict[str, int] = {}
_CURRENT_SEGMENT_INDEX = 0


def _record_new_segments_from_m3u8(m3u8_obj: Any) -> None:
    """Update the timestamp map using the absolute media sequence."""
    global _CURRENT_SEGMENT_INDEX, _SEGMENT_TIMESTAMPS
    if not hasattr(m3u8_obj, "segments"):
        return

    try:
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
        LOG.info(f"Recorded timestamp for {segment_name}, published={getattr(m3u8_obj, 'published', False)}")

        # Keep a rolling window of recent segments
        if len(_SEGMENT_TIMESTAMPS) > 120:
            oldest = sorted(_SEGMENT_TIMESTAMPS.keys())[0]
            del _SEGMENT_TIMESTAMPS[oldest]

    _CURRENT_SEGMENT_INDEX = latest_file_index


def build_ffmpeg_command(args) -> list[str]:
    """Build FFmpeg command."""
    w, h = args.resolution
    
    if args.test_src:
        inputs = ["-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={args.framerate}",
                  "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
    else:
        inputs = ["-f", "v4l2", "-video_size", f"{w}x{h}", "-framerate", str(args.framerate),
                  "-i", args.device, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "info", *inputs,
        "-vf", f"format=yuv420p,drawtext=fontsize=24:fontcolor=red:x=10:y=10:text='%{{localtime}}'",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-b:v", f"{args.bitrate}k",
        "-g", str(args.gop), "-keyint_min", str(args.gop), "-sc_threshold", "0", "-bf", "0",
        "-shortest", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-f", "mpegts", "-"
    ]


async def handle_timestamps(request: web.Request) -> web.Response:
    """Serve timestamps for latency measurement."""
    data = {
        "segments": _SEGMENT_TIMESTAMPS,
        "parts": _PART_TIMESTAMPS,
        "timestamp": time.time_ns()
    }
    return web.Response(
        text=json.dumps(data),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"}
    )


async def run_pipeline(args) -> None:
    """Run LL-HLS pipeline."""
    handler = Fmp4VariantHandler(
        target_duration=args.target_duration,
        part_target=args.part_duration,
        window_size=args.window_size,
        has_video=True, has_audio=True
    )
    
    # HTTP routes
    async def serve_client(req):
        html_path = Path(__file__).parent / "client" / "browser.html"
        if html_path.exists():
            return web.Response(text=html_path.read_text(), content_type="text/html", 
                              headers={"Access-Control-Allow-Origin": "*"})
        return web.Response(text="Client not found", status=404)
    
    app = web.Application()
    app.add_routes([
        web.get('/', serve_client),
        web.get('/playlist.m3u8', handler.playlist),
        web.get('/live.m3u8', handler.playlist),
        web.get('/segment', handler.segment),
        web.get('/part', handler.partial),
        web.get('/init', handler.initialization),
        web.get('/timestamps.json', handle_timestamps),
    ])
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port).start()
    LOG.info(f"Server running at http://0.0.0.0:{args.port}")
    
    # Start FFmpeg
    cmd = build_ffmpeg_command(args)
    LOG.info(f"FFmpeg: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    # Log FFmpeg output
    async def log_stderr():
        async for line in proc.stderr:
            LOG.info(f"FFmpeg: {line.decode().strip()}")
    asyncio.create_task(log_stderr())
    
    # MPEG-TS parsers
    pat_parser = SectionParser(PATSection)
    pmt_parser = SectionParser(PMTSection)
    aac_parser = PESParser(PES)
    h264_parser = PESParser(H264PES)
    
    pmt_pid = h264_pid = aac_pid = None
    frame_count = 0
    
    try:
        while True:
            sync = await proc.stdout.read(1)
            if not sync:
                break
            if sync != ts.SYNC_BYTE:
                continue
            
            try:
                packet = ts.SYNC_BYTE + await proc.stdout.readexactly(ts.PACKET_SIZE - 1)
            except asyncio.IncompleteReadError:
                break
            
            pid = ts.pid(packet)
            
            # PCR
            if ts.has_pcr(packet):
                handler.pcr(ts.pcr(packet))
            
            # H.264
            if pid == h264_pid:
                h264_parser.push(packet)
                for pes in h264_parser:
                    handler.h264(pes)
                    frame_count += 1
                    if frame_count <= 5 or frame_count % 100 == 0:
                        LOG.info(f"Frame {frame_count}: init={handler.init.done()}")
                    
                    # Track segments using the same logic as original
                    if hasattr(handler, 'm3u8'):
                        _record_new_segments_from_m3u8(handler.m3u8)
            
            # AAC
            elif pid == aac_pid:
                aac_parser.push(packet)
                for pes in aac_parser:
                    handler.aac(pes)
            
            # PAT
            elif pid == 0x00:
                pat_parser.push(packet)
                for pat in pat_parser:
                    if pat.CRC32() == 0:
                        for num, map_pid in pat:
                            if num != 0 and not pmt_pid:
                                pmt_pid = map_pid
                                LOG.info(f"PMT PID: {pmt_pid}")
            
            # PMT
            elif pid == pmt_pid:
                pmt_parser.push(packet)
                for pmt in pmt_parser:
                    if pmt.CRC32() == 0:
                        for stream_type, elem_pid, _ in pmt:
                            if stream_type == 0x1b and not h264_pid:
                                h264_pid = elem_pid
                                LOG.info(f"H.264 PID: {h264_pid}")
                            elif stream_type == 0x0f and not aac_pid:
                                aac_pid = elem_pid
                                LOG.info(f"AAC PID: {aac_pid}")
    
    except asyncio.CancelledError:
        LOG.info("Cancelled")
    finally:
        proc.terminate()
        await proc.wait()
        await runner.cleanup()


def main():
    p = argparse.ArgumentParser(description="LL-HLS streamer")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--test-src", action="store_true")
    p.add_argument("--resolution", default="640x360", type=lambda x: tuple(map(int, x.split('x'))))
    p.add_argument("--framerate", default=30, type=int)
    p.add_argument("--bitrate", default=1500, type=int)
    p.add_argument("--gop", default=30, type=int, help="Keyframe interval")
    p.add_argument("--target-duration", default=1, type=int)
    p.add_argument("--part-duration", default=0.1, type=float)
    p.add_argument("--window-size", default=5, type=int)
    p.add_argument("--port", default=8080, type=int)
    args = p.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    LOG.info(f"Starting LL-HLS: target={args.target_duration}s, part={args.part_duration}s")
    
    try:
        asyncio.run(run_pipeline(args))
    except KeyboardInterrupt:
        LOG.info("Stopped")


if __name__ == "__main__":
    main()
