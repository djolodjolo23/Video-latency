import argparse
import asyncio
import logging
from typing import Dict, Optional, Set

from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.sdp import candidate_from_sdp


class WebRTCPeerManager:
    def __init__(self, config: argparse.Namespace, media_source: Optional[MediaPlayer]):
        self.config = config
        self.media_source = media_source
        self.relay = MediaRelay()
        self.pcs: Set[RTCPeerConnection] = set()
        self.peers_by_sid: Dict[str, RTCPeerConnection] = {}
        self.remote_end_of_candidates: Set[str] = set()

    def _subscribe_tracks(
        self,
    ) -> tuple[Optional[MediaStreamTrack], Optional[MediaStreamTrack]]:
        audio = (
            self.relay.subscribe(self.media_source.audio)
            if self.media_source and self.media_source.audio
            else None
        )
        video = (
            self.relay.subscribe(self.media_source.video)
            if self.media_source and self.media_source.video
            else None
        )
        return audio, video

    def _force_codec(self, pc: RTCPeerConnection, sender: RTCRtpSender, codec: str):
        kind = codec.split("/")[0]
        codecs = RTCRtpSender.getCapabilities(kind).codecs
        transceiver = next(t for t in pc.getTransceivers() if t.sender == sender)
        transceiver.setCodecPreferences([c for c in codecs if c.mimeType == codec])

    async def create_peer_connection(
        self, offer: RTCSessionDescription, sid: Optional[str] = None
    ) -> RTCPeerConnection:
        pc = RTCPeerConnection()
        if sid:
            self.peers_by_sid[sid] = pc
            self.remote_end_of_candidates.discard(sid)
        self.pcs.add(pc)
        logging.info("Created peer connection %s (sid=%s)", id(pc), sid or "-")

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            logging.info("Connection state is %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self.close_peer(pc, sid=sid)

        @pc.on("track")
        def on_track(track) -> None:
            logging.info("Track %s received (%s)", track.id, track.kind)

            @track.on("ended")
            async def on_ended() -> None:
                logging.info("Track %s ended", track.id)

        audio, video = self._subscribe_tracks()
        if audio is None and video is None:
            logging.warning("No media source available; answering without media tracks")

        if audio:
            audio_sender = pc.addTrack(audio)
            if self.config.audio_codec:
                self._force_codec(pc, audio_sender, self.config.audio_codec)
            elif self.config.play_without_decoding:
                raise Exception("You must specify the audio codec using --audio-codec")

        if video:
            video_sender = pc.addTrack(video)
            if self.config.video_codec:
                self._force_codec(pc, video_sender, self.config.video_codec)
            elif self.config.play_without_decoding:
                raise Exception("You must specify the video codec using --video-codec")

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        logging.info("Sending answer from peer connection %s", id(pc))
        return pc

    async def add_candidate(self, sid: str, candidate: Optional[dict]) -> None:
        pc = self.peers_by_sid.get(sid)
        if not pc:
            logging.warning("Received ICE candidate for unknown sid %s", sid)
            return

        # Browsers signal end-of-candidates either with a null payload or an empty
        # candidate string. Once seen, ignore any further trickle to avoid
        # aioice errors about end-of-candidates.
        if candidate is None or not candidate.get("candidate"):
            if sid in self.remote_end_of_candidates:
                logging.warning("Duplicate end-of-candidates from %s", sid)
                return
            self.remote_end_of_candidates.add(sid)
            await pc.addIceCandidate(None)
            return

        if sid in self.remote_end_of_candidates:
            logging.warning("Received ICE candidate after end-of-candidates from %s", sid)
            return

        if "candidate" not in candidate:
            logging.warning("Malformed ICE candidate from %s: %s", sid, candidate)
            return

        try:
            ice_candidate = candidate_from_sdp(candidate["candidate"])
            ice_candidate.sdpMid = candidate.get("sdpMid")
            ice_candidate.sdpMLineIndex = candidate.get("sdpMLineIndex")
            await pc.addIceCandidate(ice_candidate)
        except ValueError as exc:
            if "after end-of-candidates" in str(exc).lower():
                already_marked = sid in self.remote_end_of_candidates
                self.remote_end_of_candidates.add(sid)
                if not already_marked:
                    logging.info("Peer %s sent candidate after end-of-candidates; ignoring", sid)
                return
            logging.exception("Error adding ICE candidate from %s", sid)
        except Exception:
            logging.exception("Error adding ICE candidate from %s", sid)

    async def close_peer(self, pc: RTCPeerConnection, sid: Optional[str] = None) -> None:
        if sid and self.peers_by_sid.get(sid) is pc:
            self.peers_by_sid.pop(sid, None)
        else:
            for key, value in list(self.peers_by_sid.items()):
                if value is pc:
                    self.peers_by_sid.pop(key, None)
                    break

        if pc in self.pcs:
            self.pcs.discard(pc)
        try:
            await pc.close()
        except Exception:
            logging.exception("Error closing peer connection %s", id(pc))

    async def close_for_sid(self, sid: str) -> None:
        pc = self.peers_by_sid.pop(sid, None)
        self.remote_end_of_candidates.discard(sid)
        if pc:
            await self.close_peer(pc, sid=sid)

    async def close_all(self) -> None:
        tasks = [self.close_peer(pc) for pc in list(self.pcs)]
        await asyncio.gather(*tasks, return_exceptions=True)
