#!/usr/bin/env python3
"""LL-HLS streamer using biim + FFmpeg."""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any

from aiohttp import web
import psutil

# Add local biim to path
BIIM_PATH = Path(__file__).parent / "biim"
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
_CLIENT_LAST_SEEN: Dict[str, float] = {}


class ServerMetricsMonitor:
    def __init__(
        self,
        expected_clients: int,
        connect_timeout: float,
        client_timeout: float,
        interval: float,
        duration: float,
        output_path: Path,
        proc_pid: Optional[int] = None,
    ) -> None:
        self.expected_clients = expected_clients
        self.connect_timeout = connect_timeout
        self.client_timeout = client_timeout
        self.interval = interval
        self.duration = duration
        self.output_path = output_path
        self.proc = psutil.Process(proc_pid or os.getpid())
        self._started = False
        self._start_ts = None
        self._samples: list[dict[str, float]] = []
        self._prev_net = psutil.net_io_counters()
        psutil.cpu_percent(interval=None)
        self.proc.cpu_percent(interval=None)

    def mark_client(self, client_id: str) -> None:
        _CLIENT_LAST_SEEN[client_id] = time.monotonic()

    def active_clients(self) -> int:
        now = time.monotonic()
        stale = [cid for cid, ts in _CLIENT_LAST_SEEN.items() if now - ts > self.client_timeout]
        for cid in stale:
            _CLIENT_LAST_SEEN.pop(cid, None)
        return len(_CLIENT_LAST_SEEN)

    async def run(self) -> None:
        if self.expected_clients <= 0:
            return

        start_wait = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            count = self.active_clients()
            if count >= self.expected_clients:
                self._started = True
                self._start_ts = time.time()
                start_mono = time.monotonic()
                LOG.info("All %s clients connected. Starting server metrics.", self.expected_clients)
                break
            if time.monotonic() - start_wait > self.connect_timeout:
                LOG.error("Expected %s clients not reached within %.1fs; aborting.", self.expected_clients, self.connect_timeout)
                os._exit(1)

        if self.duration <= 0:
            LOG.info("Metrics duration is %.1fs; skipping metrics collection.", self.duration)
            return

        while time.monotonic() - start_mono < self.duration:
            await asyncio.sleep(self.interval)
            self._record_sample(self.active_clients())

        LOG.info("Metrics window complete (%.1fs).", self.duration)
        self._write_summary()

    def _record_sample(self, connected: int) -> None:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        proc_cpu = self.proc.cpu_percent(interval=None) / psutil.cpu_count()
        proc_rss_mb = self.proc.memory_info().rss / (1024**2)
        net = psutil.net_io_counters()
        rx_delta = net.bytes_recv - self._prev_net.bytes_recv
        tx_delta = net.bytes_sent - self._prev_net.bytes_sent
        self._prev_net = net
        rx_kbps = (rx_delta * 8) / (self.interval * 1000)
        tx_kbps = (tx_delta * 8) / (self.interval * 1000)
        self._samples.append(
            {
                "connected": float(connected),
                "sys_cpu": cpu,
                "sys_mem": mem.percent,
                "proc_cpu": proc_cpu,
                "proc_rss_mb": proc_rss_mb,
                "rx_kbps": rx_kbps,
                "tx_kbps": tx_kbps,
            }
        )

    def _write_summary(self) -> None:
        if not self._samples or self._start_ts is None:
            return
        duration = time.time() - self._start_ts
        avg = lambda key: sum(s[key] for s in self._samples) / len(self._samples)
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": time.strftime("%Y%m%d-%H%M%S", time.localtime(self._start_ts)),
            "expected_clients": self.expected_clients,
            "connected_clients": int(max(s["connected"] for s in self._samples)),
            "duration_sec": duration,
            "avg_sys_cpu_pct": avg("sys_cpu"),
            "avg_sys_mem_pct": avg("sys_mem"),
            "avg_proc_cpu_pct": avg("proc_cpu"),
            "avg_proc_rss_mb": avg("proc_rss_mb"),
            "avg_net_rx_kbps": avg("rx_kbps"),
            "avg_net_tx_kbps": avg("tx_kbps"),
            "samples": len(self._samples),
        }
        header = (
            "timestamp,session_id,expected_clients,connected_clients,duration_sec,"
            "avg_sys_cpu_pct,avg_sys_mem_pct,avg_proc_cpu_pct,avg_proc_rss_mb,"
            "avg_net_rx_kbps,avg_net_tx_kbps,samples\n"
        )
        line = (
            f"{row['timestamp']},{row['session_id']},{row['expected_clients']},{row['connected_clients']},"
            f"{row['duration_sec']:.1f},{row['avg_sys_cpu_pct']:.2f},{row['avg_sys_mem_pct']:.2f},"
            f"{row['avg_proc_cpu_pct']:.2f},{row['avg_proc_rss_mb']:.1f},{row['avg_net_rx_kbps']:.2f},"
            f"{row['avg_net_tx_kbps']:.2f},{row['samples']}\n"
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.output_path.exists():
            self.output_path.write_text(header + line)
        else:
            with self.output_path.open("a") as f:
                f.write(line)


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
    
    monitor = ServerMetricsMonitor(
        expected_clients=args.expected_clients,
        connect_timeout=args.connect_timeout,
        client_timeout=args.client_timeout,
        interval=args.metrics_interval,
        duration=args.metrics_duration,
        output_path=Path(args.metrics_output),
    )

    # HTTP routes
    async def serve_client(req):
        html_path = Path(__file__).parent / "client" / "browser.html"
        if html_path.exists():
            return web.Response(text=html_path.read_text(), content_type="text/html", 
                              headers={"Access-Control-Allow-Origin": "*"})
        return web.Response(text="Client not found", status=404)

    @web.middleware
    async def client_tracking_middleware(request, handler):
        client_id = request.query.get("clientId")
        if client_id and client_id.startswith("warmup"):
            return await handler(request)
        if not client_id:
            client_id = request.remote or "unknown"
        monitor.mark_client(client_id)
        return await handler(request)
    
    app = web.Application()
    app.middlewares.append(client_tracking_middleware)
    app.add_routes([
        web.get('/', serve_client),
        web.get('/playlist.m3u8', handler.playlist),
        web.get('/live.m3u8', handler.playlist),
        web.get('/segment', handler.segment),
        web.get('/part', handler.partial),
        web.get('/init', handler.initialization),
        web.get('/timestamps.json', handle_timestamps),
    ])

    if args.expected_clients > 0:
        asyncio.create_task(monitor.run())
    
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
    p.add_argument("--expected-clients", type=int, default=0, help="Start metrics only after N clients connect (0 to disable)")
    p.add_argument("--connect-timeout", type=float, default=30.0, help="Abort if N clients not reached within this time (seconds)")
    p.add_argument("--client-timeout", type=float, default=10.0, help="Consider client disconnected after inactivity (seconds)")
    p.add_argument("--metrics-interval", type=float, default=1.0, help="Server metrics sampling interval in seconds")
    p.add_argument("--metrics-duration", type=float, default=30.0, help="Duration in seconds to collect server metrics once all clients connect")
    p.add_argument("--metrics-output", default="server_metrics.csv", help="Path for server metrics CSV output")
    args = p.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    LOG.info(f"Starting LL-HLS: target={args.target_duration}s, part={args.part_duration}s")
    
    try:
        asyncio.run(run_pipeline(args))
    except KeyboardInterrupt:
        LOG.info("Stopped")


if __name__ == "__main__":
    main()
