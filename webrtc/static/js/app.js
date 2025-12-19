import { createLogger } from "./logging.js";

const remoteVideo = document.getElementById("remoteVideo");
const logEl = document.getElementById("logs");
const metricsEl = document.getElementById("metrics");

const log = createLogger(logEl);
const socket = io();

let pc;
// let offerSent = false;
let statsInterval;
let videoFrameHandle;
let lastVideoBytes;
let lastVideoTimestamp;
let lastMetricsSentAt = 0;

const METRICS_PUSH_INTERVAL_MS = 2000;

const metrics = {
  sessionStart: performance.now(),
  offerSentAt: null,
  firstFrameAt: null,
  stalls: [],
  currentStallStart: null,
  totalStallMs: 0,
  stats: {},
};

const formatMs = (ms) => (typeof ms === "number" ? `${ms.toFixed(1)} ms` : "–");

const liveStallMs = () =>
  metrics.totalStallMs + (metrics.currentStallStart ? performance.now() - metrics.currentStallStart : 0);

function emitMetrics() {
  if (!socket.connected) return;
  const now = Date.now();
  if (now - lastMetricsSentAt < METRICS_PUSH_INTERVAL_MS) return;
  lastMetricsSentAt = now;

  const ttff =
    metrics.firstFrameAt && metrics.offerSentAt ? metrics.firstFrameAt - metrics.offerSentAt : null;
  const stats = metrics.stats || {};

  socket.emit("metrics", {
    ts: now,
    ttffMs: ttff,
    stallCount: metrics.stalls.length,
    stallTotalMs: liveStallMs(),
    jitterBufferDelayMs: stats.jitterBufferDelayMs,
    rttMs: stats.rttMs,
    bitrateKbps: stats.bitrateKbps,
    framesDropped: stats.framesDropped,
    framesReceived: stats.framesReceived,
  });
}

function renderMetrics() {
  if (!metricsEl) return;
  const ttff =
    metrics.firstFrameAt && metrics.offerSentAt ? metrics.firstFrameAt - metrics.offerSentAt : null;
  const stats = metrics.stats || {};
  const bitrate = typeof stats.bitrateKbps === "number" ? `${stats.bitrateKbps.toFixed(1)} kbps` : "–";
  const dropRatio =
    stats.framesDropped !== undefined && stats.framesReceived
      ? `${stats.framesDropped}/${stats.framesReceived}`
      : "–";

  metricsEl.innerHTML = `
    <div class="metric-row"><span>Time to first frame</span><span>${formatMs(ttff)}</span></div>
    <div class="metric-row"><span>Stalls</span><span>${metrics.stalls.length} (${formatMs(liveStallMs())})</span></div>
    <div class="metric-row"><span>RTT</span><span>${formatMs(stats.rttMs)}</span></div>
    <div class="metric-row"><span>Jitter buffer delay</span><span>${formatMs(stats.jitterBufferDelayMs)}</span></div>
    <div class="metric-row"><span>Video bitrate</span><span>${bitrate}</span></div>
    <div class="metric-row"><span>Frames dropped</span><span>${dropRatio}</span></div>
  `;
  emitMetrics();
}

function resetMetrics() {
  metrics.sessionStart = performance.now();
  metrics.offerSentAt = null;
  metrics.firstFrameAt = null;
  metrics.stalls = [];
  metrics.currentStallStart = null;
  metrics.totalStallMs = 0;
  metrics.stats = {};
  lastVideoBytes = undefined;
  lastVideoTimestamp = undefined;
  lastMetricsSentAt = 0;
  renderMetrics();
}

function markFirstFrame(reason) {
  if (metrics.firstFrameAt) return;
  metrics.firstFrameAt = performance.now();
  log(
    `First frame (${reason}). TTFF=${formatMs(metrics.firstFrameAt - (metrics.offerSentAt || metrics.sessionStart))}`,
  );
  renderMetrics();
  if (videoFrameHandle) {
    remoteVideo.cancelVideoFrameCallback(videoFrameHandle);
    videoFrameHandle = null;
  }
}

function watchFirstFrame() {
  if (!remoteVideo.requestVideoFrameCallback) return;
  videoFrameHandle = remoteVideo.requestVideoFrameCallback(() => markFirstFrame("frame callback"));
}

function beginStall() {
  if (metrics.currentStallStart) return;
  metrics.currentStallStart = performance.now();
  metrics.stalls.push(metrics.currentStallStart);
  log("Playback stalled/waiting...");
  renderMetrics();
}

function endStall(eventName) {
  if (!metrics.currentStallStart) return;
  const duration = performance.now() - metrics.currentStallStart;
  metrics.totalStallMs += duration;
  metrics.currentStallStart = null;
  log(`Playback resumed after ${formatMs(duration)} (${eventName})`);
  renderMetrics();
}

async function pollStats() {
  if (!pc) return;
  const reports = await pc.getStats();
  let inboundVideo;
  let candidatePair;
  reports.forEach((report) => {
    if (report.type === "inbound-rtp" && report.kind === "video" && !report.isRemote) {
      inboundVideo = report;
    }
    if (report.type === "candidate-pair" && report.state === "succeeded" && report.nominated) {
      candidatePair = report;
    }
  });

  const jitterBufferDelayMs =
    inboundVideo && inboundVideo.jitterBufferEmittedCount
      ? (inboundVideo.jitterBufferDelay / inboundVideo.jitterBufferEmittedCount) * 1000
      : null;

  let bitrateKbps;
  if (inboundVideo?.bytesReceived !== undefined && inboundVideo?.timestamp !== undefined) {
    if (lastVideoBytes !== undefined && lastVideoTimestamp !== undefined) {
      const deltaBytes = inboundVideo.bytesReceived - lastVideoBytes;
      const deltaMs = inboundVideo.timestamp - lastVideoTimestamp;
      if (deltaBytes >= 0 && deltaMs > 0) {
        bitrateKbps = (deltaBytes * 8) / deltaMs;
      }
    }
    lastVideoBytes = inboundVideo.bytesReceived;
    lastVideoTimestamp = inboundVideo.timestamp;
  }

  metrics.stats = {
    jitterBufferDelayMs,
    bitrateKbps,
    framesDropped: inboundVideo?.framesDropped,
    framesReceived: inboundVideo?.framesReceived,
    rttMs: candidatePair?.currentRoundTripTime ? candidatePair.currentRoundTripTime * 1000 : null,
  };
  renderMetrics();
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
  resetMetrics();
  const nextPc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });
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
    watchFirstFrame();
  };

  nextPc.addTransceiver("video", { direction: "recvonly" });
  nextPc.addTransceiver("audio", { direction: "recvonly" });

  nextPc.oniceconnectionstatechange = () => {
    log(`ICE connection state: ${nextPc.iceConnectionState}`);
    if (nextPc.iceConnectionState === "failed" || nextPc.iceConnectionState === "closed") {
      if (statsInterval) clearInterval(statsInterval);
    }
  };
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
    metrics.offerSentAt = performance.now();
    if (statsInterval) clearInterval(statsInterval);
    statsInterval = window.setInterval(pollStats, 1000);
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

remoteVideo.addEventListener("loadeddata", () => markFirstFrame("loadeddata"));
remoteVideo.addEventListener("playing", () => {
  markFirstFrame("playing");
  endStall("playing");
});
remoteVideo.addEventListener("waiting", beginStall);
remoteVideo.addEventListener("stalled", beginStall);
remoteVideo.addEventListener("error", () => endStall("error"));

start().catch((err) => log(`Error starting stream: ${err.message}`));
