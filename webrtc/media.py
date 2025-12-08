import argparse
import asyncio
import logging
from typing import Optional

from aiortc.contrib.media import MediaPlayer


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


def open_media_source(config: argparse.Namespace) -> Optional[MediaPlayer]:
    """Create the shared media source from file or device once at startup."""
    if config.play_from:
        player = MediaPlayer(config.play_from, decode=not config.play_without_decoding)
        _attach_player_logging(player)
        return player

    if config.video_device.lower() == "none":
        return None

    options: dict[str, str] = {}
    if config.framerate:
        options["framerate"] = str(config.framerate)
    if config.video_input_format:
        # "mjpeg" vs "yuyv422"; ffmpeg will request this pixel format from v4l2
        options["input_format"] = config.video_input_format
    if config.video_size:
        options["video_size"] = config.video_size
    player = MediaPlayer(config.video_device, format=config.video_format, options=options)
    _attach_player_logging(player)
    return player


async def stop_media_source(media_source: Optional[MediaPlayer]) -> None:
    """Stop and tear down the shared media player safely."""
    if not media_source:
        return

    for track in (media_source.audio, media_source.video):
        if track is None:
            continue
        try:
            track.stop()
        except Exception:
            logging.exception("Error stopping MediaPlayer track")

    for attr in ("stop", "close"):
        fn = getattr(media_source, attr, None)
        if callable(fn):
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logging.exception("Error calling MediaPlayer.%s()", attr)
