import { createLogger } from "./logging.js";

const remoteVideo = document.getElementById("remoteVideo");
const logEl = document.getElementById("logs");

const params = new URLSearchParams(location.search);
const CLIENT_ID = params.get("clientId") || "0";

const log = createLogger(logEl);
const socket = io();

let pc;
let qoeChannel;
// let offerSent = false;

function logFrameTimestamp(payload) {
  const pts = payload?.pts;
  const sendTimeMs = payload?.sendTimeMs;
  if (typeof pts !== "number" || typeof sendTimeMs !== "number") {
    return;
  }
  console.log(`FRAME_TS pts=${pts.toFixed(6)}s sendTimeMs=${sendTimeMs}`);
}

socket.on("connect", () => {
  log("Socket.IO connected");
});

socket.on("connect_error", (err) => {
  log(`Socket.IO connect error: ${err.message}`);
});

socket.on("answer", async (answer) => {
  log("Answer received via Socket.IO");
  if (!pc) {
    log("Ignoring answer because peer connection is gone");
    return;
  }
  await pc.setRemoteDescription(new RTCSessionDescription(answer));
  log("Stream established. You should see remote video.");
});

socket.on("offer_error", (payload) => {
  const message = payload?.message || "Unknown server error";
  log(`Server error: ${message}`);
});

function createPeerConnection() {
  const nextPc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });
  qoeChannel = nextPc.createDataChannel("qoe");
  qoeChannel.onopen = () => {
    log("Timestamp channel open");
  };
  qoeChannel.onmessage = (event) => {
    if (typeof event.data !== "string") return;
    try {
      const payload = JSON.parse(event.data);
      if (payload?.type === "frame_ts") {
        logFrameTimestamp(payload);
      }
    } catch (err) {
      console.log("FRAME_TS invalid payload", err);
    }
  };
  // offerSent = false; // reset state for new connection not implemented yet
  nextPc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit("candidate", event.candidate);
      return;
    }
    log("ICE gathering complete, sent end-of-candidates");
    socket.emit("candidate", null);
  };

  nextPc.ontrack = (event) => {
    log(`Remote track received: ${event.track.kind}`);
    remoteVideo.srcObject = event.streams[0];
    remoteVideo
      .play()
      .catch(() => log("Autoplay was blocked; click the video to start playback."));
  };

  nextPc.addTransceiver("video", { direction: "recvonly" });
  nextPc.addTransceiver("audio", { direction: "recvonly" });
  return nextPc;
}

async function start() {
  if (pc) return;
  log("Creating receive-only peer connection...");
  pc = createPeerConnection();
  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    log("Local offer created, gathering ICE...");
    sendOffer(pc.localDescription); // send immediately; ICE trickles after
  } catch (err) {
    log(`Error starting stream: ${err.message}`);
    // closePeerConnection();
  }
}

function sendOffer(desc) {
  // if (offerSent) return;
  log("Sending offer via Socket.IO...");
  socket.emit("offer", { sdp: desc.sdp, type: desc.type });
  // offerSent = true;
}

/*
This close peer connection function is not currently used.
Idk what the purpose of closing down a peer connection could be used for
if pausing a video should not close the connection but simply pause the video.
This is commented out with the closePeerConnection call in start() on error as well.
Removal of a stopping peer connection also forces offersent constant to be removed
because it's function is to prevent duplicate offers being sent on a single peer connection,
when shutting down and restarting. 
*/
// function closePeerConnection() {
//   if (!pc) return;
//   pc.getSenders().forEach((sender) => sender.track && sender.track.stop());
//   pc.close();
//   pc = null;
//   // offerSent = false;
//   remoteVideo.srcObject = null;
// }

// async function stop() {
//   if (!pc) return;
//   log("Stopping peer connection...");
//   try {
//     closePeerConnection();
//   } catch (e) {
//     log("Error while closing peer connection");
//   }
// }

remoteVideo.addEventListener("click", () => {
  if (remoteVideo.srcObject) {
    remoteVideo.play().catch(() => log("Playback failed to start from click."));
    return;
  }
  start().catch((err) => log(`Error starting stream: ${err.message}`));
});

start().catch((err) => log(`Error starting stream: ${err.message}`));
