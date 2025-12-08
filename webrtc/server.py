import logging
import ssl
from pathlib import Path

import socketio
from aiohttp import web
from aiortc import RTCSessionDescription

from cli import parse_args
from media import open_media_source, stop_media_source
from webrtc import WebRTCPeerManager


ROOT = Path(__file__).parent


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(ROOT / "static" / "index.html")


def create_app(config, source=None) -> web.Application:
    """Wire up aiohttp + Socket.IO with WebRTC helpers."""
    media_source = source if source is not None else open_media_source(config)
    peer_manager = WebRTCPeerManager(config, media_source)
    sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")

    app = web.Application()
    app["config"] = config
    app["media_source"] = media_source
    app["peer_manager"] = peer_manager
    app["sio"] = sio
    sio.attach(app)

    async def offer(request: web.Request) -> web.Response:
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        pc = await peer_manager.create_peer_connection(offer)
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

    @sio.event
    async def connect(sid, environ) -> None:
        logging.info("Socket connected %s", sid)

    @sio.event
    async def disconnect(sid) -> None:
        logging.info("Socket disconnected %s", sid)
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
        pc = await peer_manager.create_peer_connection(offer, sid=sid)
        await sio.emit(
            "answer",
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            to=sid,
        )

    @sio.on("candidate")  # type: ignore
    async def on_socket_candidate(sid, data) -> None:
        await peer_manager.add_candidate(sid, data)

    async def on_shutdown(app: web.Application) -> None:
        try:
            await sio.shutdown()
        except Exception:
            logging.exception("Error during Socket.IO shutdown")

        await stop_media_source(media_source)
        await peer_manager.close_all()

    app.on_shutdown.append(on_shutdown)
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
