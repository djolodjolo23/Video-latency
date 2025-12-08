import argparse
import asyncio
import json
import logging
import os
import socketio
from pathlib import Path

import av.logging as av_logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay

ROOT = Path(__file__).parent
pcs = set()
relay = MediaRelay()
player_source: MediaPlayer | None = None
sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")


async def index(request: web.Request) -> web.Response:
    """Serve the demo page."""
    return web.FileResponse(ROOT / "static" / "index.html")


def _attach_player_logging(player: MediaPlayer) -> None:
    """Log capture start/end events for easier debugging."""
    if player.audio:
        @player.audio.on("ended")
        async def on_audio_end() -> None:
            logging.error("Server audio track ended unexpectedly")

    if player.video:
        @player.video.on("ended")
        async def on_video_end() -> None:
            logging.error("Server video track ended unexpectedly")


async def _create_peer_connection(offer: RTCSessionDescription) -> RTCPeerConnection:
    """Create a peer connection, attach server-side media, and return it."""
    pc = RTCPeerConnection()
    pcs.add(pc)
    logging.info("Created peer connection %s", id(pc))

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        logging.info("Connection state is %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await cleanup_peer(pc)

    @pc.on("track")
    def on_track(track) -> None:
        logging.info("Track %s received (%s)", track.id, track.kind)

        @track.on("ended")
        async def on_ended() -> None:
            logging.info("Track %s ended", track.id)

    if player_source:
        if player_source.audio:
            pc.addTrack(relay.subscribe(player_source.audio))
        if player_source.video:
            pc.addTrack(relay.subscribe(player_source.video))

    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logging.info("Sending answer from peer connection %s", id(pc))
    return pc


async def offer(request: web.Request) -> web.Response:
    """Handle an SDP offer from the browser and return an answer."""
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = await _create_peer_connection(offer)
    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


@sio.event
async def connect(sid, environ) -> None:
    logging.info("Socket connected %s", sid)


@sio.event
async def disconnect(sid) -> None:
    logging.info("Socket disconnected %s", sid)


@sio.on("offer")
async def on_socket_offer(sid, data) -> None:
    try:
        offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
    except Exception:
        logging.exception("Invalid offer payload from %s", sid)
        await sio.emit("offer_error", {"message": "Invalid offer payload"}, to=sid)
        return

    pc = await _create_peer_connection(offer)
    await sio.emit(
        "answer",
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        to=sid,
    )


async def cleanup_peer(pc: RTCPeerConnection) -> None:
    if pc in pcs:
        pcs.discard(pc)
    await pc.close()


async def on_shutdown(app: web.Application) -> None:
    coros = [cleanup_peer(pc) for pc in pcs]
    await asyncio.gather(*coros)


def create_app(player: MediaPlayer | None = None) -> web.Application:
    global player_source
    player_source = player
    app = web.Application()
    app["player"] = player
    sio.attach(app)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_static("/static/", ROOT / "static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal WebRTC loopback using aiortc + aiohttp"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Listening host")
    parser.add_argument("--port", type=int, default=8080, help="Listening port")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level for server and PyAV (unless PYAV_LOGLEVEL is set)",
    )
    parser.add_argument(
        "--video-device",
        default="/dev/video0",
        help="Video device path on the server (v4l2). Set to 'none' to disable.",
    )
    parser.add_argument(
        "--video-size",
        # default="1280x720",  # ERROR:libav.rawvideo:Invalid buffer size, packet size 614400 < expected frame_size 1843200
        default="640x480",
        help="Optional WxH passed to ffmpeg for the capture device.",
    )
    parser.add_argument(
        "--video-format",
        default="v4l2",
        help="ffmpeg input format for the capture device.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    pyav_level = os.getenv("PYAV_LOGLEVEL", args.log_level).upper()
    if pyav_level:
        try:
            level = getattr(av_logging, pyav_level, av_logging.INFO)
            av_logging.set_level(level)
            logging.info("PyAV logging enabled at %s", pyav_level)
        except Exception as exc:
            logging.warning("Could not enable PyAV logging: %s", exc)

    player = None
    if args.video_device.lower() != "none":
        video_options = {}
        if args.video_size:
            video_options["video_size"] = args.video_size
        player = MediaPlayer(
            args.video_device, format=args.video_format, options=video_options
        )
        logging.info(
            "Opening video device %s with format=%s size=%s",
            args.video_device,
            args.video_format,
            args.video_size,
        )
        _attach_player_logging(player)

    web.run_app(create_app(player), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
