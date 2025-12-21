import asyncio
import logging
import os
import ssl
import time
from pathlib import Path
from urllib.parse import parse_qs

import socketio
from aiohttp import web
from aiortc import RTCSessionDescription
import psutil

from cli import parse_args
from media import open_media_source, stop_media_source
from webrtc import WebRTCPeerManager


ROOT = Path(__file__).parent


class ServerMetricsMonitor:
    def __init__(
        self,
        expected_clients: int,
        connect_timeout: float,
        interval: float,
        duration: float,
        output_path: Path,
    ) -> None:
        self.expected_clients = expected_clients
        self.connect_timeout = connect_timeout
        self.interval = interval
        self.duration = duration
        self.output_path = output_path
        self.proc = psutil.Process(os.getpid())
        self._prev_net = psutil.net_io_counters()
        self._connected: set[str] = set()
        self._sid_to_client_id: dict[str, str] = {}
        self._started = False
        self._start_ts: float | None = None
        self._samples: list[dict[str, float]] = []
        psutil.cpu_percent(interval=None)
        self.proc.cpu_percent(interval=None)

    def register_sid(self, sid: str, client_id: str | None) -> None:
        if client_id:
            self._sid_to_client_id[sid] = client_id

    def unregister_sid(self, sid: str) -> None:
        self._sid_to_client_id.pop(sid, None)

    def mark_connected(self, sid: str) -> None:
        if self._is_warmup(sid):
            return
        self._connected.add(sid)

    def mark_disconnected(self, sid: str) -> None:
        self._connected.discard(sid)

    def connected_count(self) -> int:
        return len(self._connected)

    def _is_warmup(self, sid: str) -> bool:
        client_id = self._sid_to_client_id.get(sid, "")
        return client_id.startswith("warmup")

    async def run(self) -> None:
        if self.expected_clients <= 0:
            return

        start_wait = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            count = self.connected_count()
            if count >= self.expected_clients:
                self._started = True
                self._start_ts = time.time()
                start_mono = time.monotonic()
                logging.info("All %s clients connected. Starting server metrics.", self.expected_clients)
                break
            if time.monotonic() - start_wait > self.connect_timeout:
                logging.error(
                    "Expected %s clients not reached within %.1fs; aborting.",
                    self.expected_clients,
                    self.connect_timeout,
                )
                os._exit(1)

        if self.duration <= 0:
            logging.info("Metrics duration is %.1fs; skipping metrics collection.", self.duration)
            return

        while time.monotonic() - start_mono < self.duration:
            await asyncio.sleep(self.interval)
            self._record_sample(self.connected_count())

        logging.info("Metrics window complete (%.1fs).", self.duration)
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


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(ROOT / "static" / "index.html")


def create_app(config, source=None) -> web.Application:
    """Wire up aiohttp + Socket.IO with WebRTC helpers."""
    media_source = source if source is not None else open_media_source(config)
    peer_manager = WebRTCPeerManager(config, media_source)
    sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
    monitor = ServerMetricsMonitor(
        expected_clients=config.expected_clients,
        connect_timeout=config.connect_timeout,
        interval=config.metrics_interval,
        duration=config.metrics_duration,
        output_path=Path(config.metrics_output),
    )

    app = web.Application()
    app["config"] = config
    app["media_source"] = media_source
    app["peer_manager"] = peer_manager
    app["sio"] = sio
    sio.attach(app)

    def _client_id_from_environ(environ: dict) -> str | None:
        query_string = environ.get("QUERY_STRING") or ""
        if not query_string:
            return None
        params = parse_qs(query_string)
        client_ids = params.get("clientId") or params.get("clientid")
        return client_ids[0] if client_ids else None

    async def offer(request: web.Request) -> web.Response:
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        pc = await peer_manager.create_peer_connection(offer)
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

    @sio.event
    async def connect(sid, environ) -> None:
        client_id = _client_id_from_environ(environ)
        monitor.register_sid(sid, client_id)
        logging.info("Socket connected %s", sid)

    @sio.event
    async def disconnect(sid) -> None:
        logging.info("Socket disconnected %s", sid)
        monitor.mark_disconnected(sid)
        monitor.unregister_sid(sid)
        await peer_manager.close_for_sid(sid)

    @sio.on("offer")  # type: ignore
    async def on_socket_offer(sid, data) -> None:
        try:
            offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
        except Exception:
            logging.exception("Invalid offer payload from %s", sid)
            await sio.emit("offer_error", {"message": "Invalid offer payload"}, to=sid)
            return

        await peer_manager.close_for_sid(sid)
        def _state_callback(_pc, state, cb_sid):
            if not cb_sid:
                return
            if state == "connected":
                monitor.mark_connected(cb_sid)
            elif state in ("failed", "closed", "disconnected"):
                monitor.mark_disconnected(cb_sid)

        pc = await peer_manager.create_peer_connection(offer, sid=sid, state_callback=_state_callback)
        await sio.emit(
            "answer",
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            to=sid,
        )

    @sio.on("candidate")  # type: ignore
    async def on_socket_candidate(sid, data) -> None:
        await peer_manager.add_candidate(sid, data)

    async def on_startup(app: web.Application) -> None:
        if config.expected_clients > 0:
            asyncio.create_task(monitor.run())

    async def on_shutdown(app: web.Application) -> None:
        try:
            await sio.shutdown()
        except Exception:
            logging.exception("Error during Socket.IO shutdown")

        await stop_media_source(media_source)
        await peer_manager.close_all()

    app.on_shutdown.append(on_shutdown)
    app.on_startup.append(on_startup)
    app.router.add_post("/offer", offer)
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "static")
    return app


def main() -> None:
    args = parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    source = open_media_source(args)
    if source:
        logging.info(
            "Using media source (audio=%s, video=%s)",
            bool(source.audio),
            bool(source.video),
        )
    else:
        logging.warning("No media source configured; answers will carry no media tracks")

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    web.run_app(
        create_app(args, source),
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
        shutdown_timeout=1.0,
    )


if __name__ == "__main__":
    main()
