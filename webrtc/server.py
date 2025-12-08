import argparse
import asyncio
import logging
import platform
import ssl
from pathlib import Path
from typing import Optional
from cli import parse_args

import socketio
from aiohttp import web
from aiortc import (
    MediaStreamTrack,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaPlayer, MediaRelay

ROOT = Path(__file__).parent
pcs = set()
relay = MediaRelay()
player: MediaPlayer | None = None
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


def create_local_tracks(play_from: str | None, decode: bool) -> tuple[Optional[MediaStreamTrack], Optional[MediaStreamTrack]]:
    global relay, player

    if play_from: # If a file name was given, play from that file.
        player = MediaPlayer(play_from, decode=decode)
        return player.audio, player.video
    else:
        # Otherwise, play from the system's default player/webcam.
        # In order to serve the same player to multiple users we make use of
        # a `MediaRelay`. The player will stay open, so it is our responsability
        # to stop the player when the application shuts down in `on_shutdown`.
        options = {"framerate": "30", "video_size": "640x480"}
        if player is None:
            if platform.system() == "Darwin":
                player = MediaPlayer(
                    "default:none", format="avfoundation", options=options
                )
            elif platform.system() == "Windows":
                player = MediaPlayer(
                    "video=Integrated Camera", format="dshow", options=options
                )
            else:
                player = MediaPlayer("/dev/video0", format="v4l2", options=options)
        return None, relay.subscribe(player.video)

def force_codec(pc: RTCPeerConnection, sender: RTCRtpSender, forced_codec: str) -> None:
    kind = forced_codec.split("/")[0]
    codecs = RTCRtpSender.getCapabilities(kind).codecs
    transceiver = next(t for t in pc.getTransceivers() if t.sender == sender)
    transceiver.setCodecPreferences(
        [codec for codec in codecs if codec.mimeType == forced_codec]
    )

async def _create_peer_connection(
    offer: RTCSessionDescription, config: argparse.Namespace
) -> RTCPeerConnection:
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

    if config.play_from:
        audio, video = create_local_tracks(
            config.play_from, decode=not config.play_without_decoding
        )
    elif player_source:
        audio = relay.subscribe(player_source.audio) if player_source.audio else None
        video = relay.subscribe(player_source.video) if player_source.video else None
    else:
        audio, video = create_local_tracks(
            play_from=None, decode=not config.play_without_decoding
        )

    if audio:
        audio_sender = pc.addTrack(audio)
        if config.audio_codec:
            force_codec(pc, audio_sender, config.audio_codec)
        elif config.play_without_decoding:
            raise Exception("You must specify the audio codec using --audio-codec")

    if video:
        video_sender = pc.addTrack(video)
        if config.video_codec:
            force_codec(pc, video_sender, config.video_codec)
        elif config.play_without_decoding:
            raise Exception("You must specify the video codec using --video-codec")

    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logging.info("Sending answer from peer connection %s", id(pc))
    return pc

async def offer(request: web.Request) -> web.Response:
    """Handle an SDP offer from the browser and return an answer."""
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    config = request.app["config"]
    pc = await _create_peer_connection(offer, config)
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

    config = getattr(sio, "ext_config")
    pc = await _create_peer_connection(offer, config)
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


def create_app(config: argparse.Namespace, player: MediaPlayer | None = None) -> web.Application:
    global player_source
    player_source = player
    app = web.Application()
    app["player"] = player
    app["config"] = config
    sio.ext_config = config  # type: ignore[attr-defined]
    sio.attach(app)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_static("/static/", ROOT / "static")
    return app


def main() -> None:
    args = parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

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

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    web.run_app(
        create_app(args, player),
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
    )

if __name__ == "__main__":
    main()
