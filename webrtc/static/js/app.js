import { createLogger } from "./logging.js";

const remoteVideo = document.getElementById("remoteVideo");
const logEl = document.getElementById("logs");
const qoeEl = document.getElementById("qoeStats");
const DEBUG_QOE = new URLSearchParams(window.location.search).has("qoeDebug");
const USE_JITTER_LATENCY = false;
const FRAME_CALLBACK_DELAY_MS = 250;
const FRAME_CALLBACK_FALLBACK_MS = 5000;
const MAX_FRAME_TS_LAG_MS = 200;

const params = new URLSearchParams(location.search);
const CLIENT_ID = params.get("clientId") || "0";

const log = createLogger(logEl);
const socket = io({ query: { clientId: CLIENT_ID } });

remoteVideo.muted = true;

let pc;
let qoeTimer;
let qoeChannel;
let timeSyncTimer;
let hasShutdown = false;
const frameQueue = [];
const FRAME_MATCH_TOLERANCE_SEC = 0.05;
let lastRenderedFrame = null;
// let offerSent = false;

const qoeState = {
  startTime: performance.now(),
  clientId: CLIENT_ID,
  ttffMs: null,
  firstFrameTime: null,
  hasStarted: false,
  isPlaying: false,
  isStalling: false,
  stallCount: 0,
  totalStallDurationMs: 0,
  currentStallStart: null,
  currentLatencyMs: null,
  latencySource: "none",
  latencySamples: [],
  rttMs: null,
  clockOffsetMs: null,
  prevJitterBufferDelay: null,
  prevJitterBufferEmittedCount: null,
  lastFrameCallbackMs: null,
  forceJitterUntilMs: 0,
};

function emitQoE(type, data = {}) {
  const payload = {
    type,
    clientId: CLIENT_ID,
    timestamp: Date.now(),
    elapsedMs: performance.now() - qoeState.startTime,
    ...data,
  };
  console.log(`QOE ${JSON.stringify(payload)}`);
}

function resetQoEState() {
  qoeState.startTime = performance.now();
  qoeState.ttffMs = null;
  qoeState.firstFrameTime = null;
  qoeState.hasStarted = false;
  qoeState.isPlaying = false;
  qoeState.isStalling = false;
  qoeState.stallCount = 0;
  qoeState.totalStallDurationMs = 0;
  qoeState.currentStallStart = null;
  qoeState.currentLatencyMs = null;
  qoeState.latencySource = "none";
  qoeState.latencySamples = [];
  qoeState.rttMs = null;
  qoeState.clockOffsetMs = null;
  qoeState.prevJitterBufferDelay = null;
  qoeState.prevJitterBufferEmittedCount = null;
  qoeState.lastFrameCallbackMs = null;
  qoeState.forceJitterUntilMs = 0;
  frameQueue.length = 0;
  lastRenderedFrame = null;
  updateQoEPanel();
}

function shouldUseJitter() {
  return USE_JITTER_LATENCY || Date.now() < qoeState.forceJitterUntilMs;
}

function formatMs(value, fallback = "-") {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return `${value.toFixed(0)} ms`;
}

function updateQoEPanel() {
  if (!qoeEl) return;
  const avgLatency =
    qoeState.latencySamples.length > 0
      ? qoeState.latencySamples.reduce((a, b) => a + b, 0) / qoeState.latencySamples.length
      : null;
  qoeEl.textContent = `TTFF: ${formatMs(qoeState.ttffMs, "waiting...")}
Latency (${qoeState.latencySource}): ${formatMs(qoeState.currentLatencyMs)}
Avg latency: ${formatMs(avgLatency)}
RTT: ${formatMs(qoeState.rttMs)}
Clock offset: ${formatMs(qoeState.clockOffsetMs)}
Stalls: ${qoeState.stallCount} (${qoeState.totalStallDurationMs.toFixed(0)} ms total)
Status: ${qoeState.isStalling ? "⏸ STALLING" : qoeState.isPlaying ? "▶ Playing" : "⏹ Stopped"}`;
}

function markFirstFrame() {
  if (qoeState.hasStarted) return;
  qoeState.ttffMs = performance.now() - qoeState.startTime;
  qoeState.firstFrameTime = performance.now();
  qoeState.hasStarted = true;
  emitQoE("ttff", { ttffMs: qoeState.ttffMs });
}

function handleStallStart() {
  if (!qoeState.hasStarted || qoeState.isStalling) return;
  qoeState.isStalling = true;
  qoeState.currentStallStart = performance.now();
  qoeState.stallCount += 1;
  emitQoE("stall_start", { stallCount: qoeState.stallCount });
}

function handleStallEnd() {
  if (!qoeState.isStalling || qoeState.currentStallStart === null) return;
  const stallDuration = performance.now() - qoeState.currentStallStart;
  qoeState.totalStallDurationMs += stallDuration;
  qoeState.isStalling = false;
  qoeState.currentStallStart = null;
  emitQoE("stall_end", { stallDurationMs: stallDuration });
}

function recordLatencySample(latencyMs, source) {
  const forceJitter = shouldUseJitter();
  if (forceJitter && source !== "jitter") {
    return;
  }
  if (source === "jitter" && qoeState.latencySource === "e2e" && !forceJitter) {
    return;
  }
  if (source === "e2e") {
    qoeState.latencySource = "e2e";
  } else if (qoeState.latencySource === "none" || forceJitter) {
    qoeState.latencySource = "jitter";
  }
  qoeState.currentLatencyMs = latencyMs;
  qoeState.latencySamples.push(latencyMs);
  if (qoeState.latencySamples.length > 60) {
    qoeState.latencySamples.shift();
  }
  emitQoE("latency", { latencyMs, source, rttMs: qoeState.rttMs });
}

function updateClockOffset(offsetMs) {
  if (qoeState.clockOffsetMs === null) {
    qoeState.clockOffsetMs = offsetMs;
    return;
  }
  const alpha = 0.2;
  qoeState.clockOffsetMs = qoeState.clockOffsetMs * (1 - alpha) + offsetMs * alpha;
}

function handleFrameTimestamp(payload) {
  if (shouldUseJitter()) {
    return;
  }
  const pts = payload?.pts;
  const sendTimeMs = payload?.sendTimeMs;
  if (typeof pts !== "number" || typeof sendTimeMs !== "number") {
    return;
  }
  const nowMs = Date.now();
  const deltaMs = nowMs - sendTimeMs;
  if (deltaMs > MAX_FRAME_TS_LAG_MS) {
    if (DEBUG_QOE) {
      console.log(
        "[QOE DEBUG] frame_ts dropped",
        JSON.stringify({
          clientNowMs: nowMs,
          sendTimeMs,
          lagMs: deltaMs,
          clockOffsetMs: qoeState.clockOffsetMs,
          queueSize: frameQueue.length,
        })
      );
    }
    return;
  }
  if (DEBUG_QOE) {
    const nowMs = Date.now();
    const deltaMs = nowMs - sendTimeMs;
    if (deltaMs > 1000) {
      console.log(
        "[QOE DEBUG] frame_ts lag",
        JSON.stringify({
          clientNowMs: nowMs,
          sendTimeMs,
          lagMs: deltaMs,
          clockOffsetMs: qoeState.clockOffsetMs,
          queueSize: frameQueue.length,
        })
      );
    }
  }
  if (lastRenderedFrame && Math.abs(lastRenderedFrame.mediaTime - pts) <= FRAME_MATCH_TOLERANCE_SEC) {
    const offsetMs = qoeState.clockOffsetMs || 0;
    const latencyMs = lastRenderedFrame.renderTimeMs - (sendTimeMs + offsetMs);
    if (DEBUG_QOE) {
      console.log(
        "[QOE DEBUG] e2e latency (handleFrameTimestamp)",
        JSON.stringify({
          renderTimeMs: lastRenderedFrame.renderTimeMs,
          sendTimeMs,
          offsetMs,
          latencyMs,
          pts,
        })
      );
    }
    recordLatencySample(latencyMs, "e2e");
    return;
  }
  frameQueue.push({ pts, sendTimeMs });
  if (frameQueue.length > 600) {
    frameQueue.shift();
  }
}

function handleTimeSyncReply(payload) {
  const t1 = payload?.clientSendMs;
  const t2 = payload?.serverRecvMs;
  const t3 = payload?.serverSendMs;
  const t4 = Date.now();
  if ([t1, t2, t3].some((v) => typeof v !== "number")) {
    return;
  }
  const offsetMs = ((t2 - t1) + (t3 - t4)) / 2;
  if (DEBUG_QOE) {
    console.log(
      "[QOE DEBUG] time_sync",
      JSON.stringify({
        t1,
        t2,
        t3,
        t4,
        offsetMs,
        rttMs: (t4 - t1) - (t3 - t2),
      })
    );
  }
  updateClockOffset(offsetMs);
}

function sendTimeSync() {
  if (!qoeChannel || qoeChannel.readyState !== "open") return;
  const payload = { type: "time_sync", clientSendMs: Date.now() };
  qoeChannel.send(JSON.stringify(payload));
}

function shutdownConnection() {
  if (hasShutdown) return;
  hasShutdown = true;

  if (timeSyncTimer) {
    clearInterval(timeSyncTimer);
    timeSyncTimer = null;
  }
  if (qoeTimer) {
    clearInterval(qoeTimer);
    qoeTimer = null;
  }
  if (qoeChannel) {
    try {
      qoeChannel.close();
    } catch (err) {
      log(`QoE channel close failed: ${err.message}`);
    }
    qoeChannel = null;
  }
  if (pc) {
    pc.getTransceivers().forEach((transceiver) => {
      if (transceiver.sender && transceiver.sender.track) {
        transceiver.sender.track.stop();
      }
      if (transceiver.receiver && transceiver.receiver.track) {
        transceiver.receiver.track.stop();
      }
    });
    try {
      pc.close();
    } catch (err) {
      log(`Peer connection close failed: ${err.message}`);
    }
    pc = null;
  }
  if (remoteVideo.srcObject) {
    remoteVideo.srcObject = null;
  }
  qoeState.isPlaying = false;
  if (socket && socket.connected) {
    socket.disconnect();
  }
}

window.__qoeShutdown = shutdownConnection;
window.addEventListener("pagehide", shutdownConnection);
window.addEventListener("beforeunload", shutdownConnection);

function onVideoFrame(_now, metadata) {
  const renderTimeMs = Date.now();
  if (qoeState.lastFrameCallbackMs !== null) {
    const deltaMs = renderTimeMs - qoeState.lastFrameCallbackMs;
    if (deltaMs > FRAME_CALLBACK_DELAY_MS) {
      qoeState.forceJitterUntilMs = Date.now() + FRAME_CALLBACK_FALLBACK_MS;
      if (DEBUG_QOE) {
        console.log(
          "[QOE DEBUG] frame callback delay",
          JSON.stringify({
            deltaMs,
            forceJitterUntilMs: qoeState.forceJitterUntilMs,
          })
        );
      }
    }
  }
  qoeState.lastFrameCallbackMs = renderTimeMs;
  const mediaTime = metadata.mediaTime;
  lastRenderedFrame = { mediaTime, renderTimeMs };
  if (!qoeState.hasStarted) {
    markFirstFrame();
  }

  while (frameQueue.length && frameQueue[0].pts < mediaTime - FRAME_MATCH_TOLERANCE_SEC) {
    frameQueue.shift();
  }
  if (frameQueue.length && Math.abs(frameQueue[0].pts - mediaTime) <= FRAME_MATCH_TOLERANCE_SEC) {
    const match = frameQueue.shift();
    const offsetMs = qoeState.clockOffsetMs || 0;
    const matchLagMs = renderTimeMs - match.sendTimeMs;
    if (matchLagMs > MAX_FRAME_TS_LAG_MS) {
      if (DEBUG_QOE) {
        console.log(
          "[QOE DEBUG] frame_ts match dropped",
          JSON.stringify({
            clientNowMs: renderTimeMs,
            sendTimeMs: match.sendTimeMs,
            lagMs: matchLagMs,
            clockOffsetMs: offsetMs,
            pts: match.pts,
            mediaTime,
            queueSize: frameQueue.length,
          })
        );
      }
      return;
    }
    const latencyMs = renderTimeMs - (match.sendTimeMs + offsetMs);
    if (DEBUG_QOE) {
      console.log(
        "[QOE DEBUG] e2e latency (onVideoFrame)",
        JSON.stringify({
          renderTimeMs,
          sendTimeMs: match.sendTimeMs,
          offsetMs,
          latencyMs,
          pts: match.pts,
          mediaTime,
        })
      );
    }
    recordLatencySample(latencyMs, "e2e");
  }

  if (typeof remoteVideo.requestVideoFrameCallback === "function") {
    remoteVideo.requestVideoFrameCallback(onVideoFrame);
  }
}

async function collectStats() {
  if (!pc) {
    updateQoEPanel();
    return;
  }
  try {
    const stats = await pc.getStats();
    let jitterBufferDelayMs = null;
    let jitterBufferDelay = null;
    let jitterBufferEmittedCount = null;
    let selectedPairId = null;

    stats.forEach((report) => {
      if (report.type === "inbound-rtp" && report.kind === "video" && !report.isRemote) {
        if (
          typeof report.jitterBufferDelay === "number" &&
          typeof report.jitterBufferEmittedCount === "number" &&
          report.jitterBufferEmittedCount > 0
        ) {
          jitterBufferDelay = report.jitterBufferDelay;
          jitterBufferEmittedCount = report.jitterBufferEmittedCount;
          if (
            qoeState.prevJitterBufferDelay !== null &&
            qoeState.prevJitterBufferEmittedCount !== null
          ) {
            const deltaDelay = jitterBufferDelay - qoeState.prevJitterBufferDelay;
            const deltaCount = jitterBufferEmittedCount - qoeState.prevJitterBufferEmittedCount;
            if (deltaCount > 0 && deltaDelay >= 0) {
              jitterBufferDelayMs = (deltaDelay / deltaCount) * 1000;
            }
          }
          qoeState.prevJitterBufferDelay = jitterBufferDelay;
          qoeState.prevJitterBufferEmittedCount = jitterBufferEmittedCount;
        }
      }
      if (report.type === "transport" && report.selectedCandidatePairId) {
        selectedPairId = report.selectedCandidatePairId;
      }
    });

    if (selectedPairId && stats.get(selectedPairId)) {
      const pair = stats.get(selectedPairId);
      if (pair && pair.currentRoundTripTime !== undefined && pair.currentRoundTripTime !== null) {
        qoeState.rttMs = pair.currentRoundTripTime * 1000;
      }
    }

    if (jitterBufferDelayMs !== null) {
      recordLatencySample(jitterBufferDelayMs, "jitter");
    }

    const avgLatency =
      qoeState.latencySamples.length > 0
        ? qoeState.latencySamples.reduce((a, b) => a + b, 0) / qoeState.latencySamples.length
        : null;

    emitQoE("stats", {
      ttffMs: qoeState.ttffMs,
      currentLatencyMs: qoeState.currentLatencyMs,
      avgLatencyMs: avgLatency,
      stallCount: qoeState.stallCount,
      totalStallDurationMs: qoeState.totalStallDurationMs,
      isStalling: qoeState.isStalling,
      isPlaying: qoeState.isPlaying,
      rttMs: qoeState.rttMs,
      latencySource: qoeState.latencySource,
      clockOffsetMs: qoeState.clockOffsetMs,
    });
    updateQoEPanel();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log(`Stats collection error: ${message}`);
  }
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
    log("QoE data channel opened");
    sendTimeSync();
    if (timeSyncTimer) {
      clearInterval(timeSyncTimer);
    }
    timeSyncTimer = setInterval(sendTimeSync, 5000);
  };
  qoeChannel.onclose = () => {
    log("QoE data channel closed");
    if (timeSyncTimer) {
      clearInterval(timeSyncTimer);
      timeSyncTimer = null;
    }
  };
  qoeChannel.onmessage = (event) => {
    if (typeof event.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (payload.type === "frame_ts") {
      handleFrameTimestamp(payload);
    } else if (payload.type === "time_sync_reply") {
      handleTimeSyncReply(payload);
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
    if (typeof remoteVideo.requestVideoFrameCallback === "function") {
      remoteVideo.requestVideoFrameCallback(onVideoFrame);
    }
  };

  nextPc.addTransceiver("video", { direction: "recvonly" });
  nextPc.addTransceiver("audio", { direction: "recvonly" });
  return nextPc;
}

async function start() {
  if (pc) return;
  resetQoEState();
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

remoteVideo.addEventListener("playing", () => {
  qoeState.isPlaying = true;
  if (!qoeState.hasStarted) {
    markFirstFrame();
  }
  handleStallEnd();
});

remoteVideo.addEventListener("waiting", () => {
  handleStallStart();
});

remoteVideo.addEventListener("stalled", () => {
  handleStallStart();
});

remoteVideo.addEventListener("pause", () => {
  qoeState.isPlaying = false;
});

remoteVideo.addEventListener("error", () => {
  emitQoE("error", { error: remoteVideo.error?.message || "unknown" });
});

if (!qoeTimer) {
  qoeTimer = setInterval(collectStats, 1000);
}

start().catch((err) => log(`Error starting stream: ${err.message}`));
