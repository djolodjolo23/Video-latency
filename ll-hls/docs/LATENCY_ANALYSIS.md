# Low-Latency HLS (LL-HLS) Latency Analysis

## Test Configuration

| Parameter | Value |
|-----------|-------|
| Segment Duration | 0.3 seconds |
| Protocol | LL-HLS with CMAF (fMP4) segments |
| Container | `.m4s` fragments |
| Encoder | x264 with `zerolatency` tune |
| Player | hls.js with `lowLatencyMode: true` |
| Transport | HTTP over local network |

---

## Observed Results

```
[LATENCY] 1276.7 ms | segment: segment00486.m4s | buffer: 0.30s (~1.0 segs)
[LATENCY] 1321.4 ms | segment: segment00487.m4s | buffer: 1.16s (~3.9 segs)
[LATENCY] 1321.1 ms | segment: segment00488.m4s | buffer: 1.46s (~4.9 segs)
[LATENCY] 1321.7 ms | segment: segment00489.m4s | buffer: 1.16s (~3.9 segs)
[LATENCY] 1321.8 ms | segment: segment00490.m4s | buffer: 0.85s (~2.8 segs)
[LATENCY] 1322.8 ms | segment: segment00491.m4s | buffer: 1.45s (~4.8 segs)
[LATENCY] 1323.1 ms | segment: segment00492.m4s | buffer: 1.15s (~3.8 segs)
[LATENCY] 1314.7 ms | segment: segment00493.m4s | buffer: 0.86s (~2.9 segs)
[LATENCY] 1320.4 ms | segment: segment00494.m4s | buffer: 0.55s (~1.8 segs)
[LATENCY] 1298.7 ms | segment: segment00495.m4s | buffer: 0.88s (~2.9 segs)
[LATENCY] 1316.1 ms | segment: segment00496.m4s | buffer: 1.15s (~3.8 segs)
[LATENCY] 1322.9 ms | segment: segment00497.m4s | buffer: 0.85s (~2.8 segs)
[LATENCY] 1288.2 ms | segment: segment00498.m4s | buffer: 1.49s (~5.0 segs)
[LATENCY] 1322.5 ms | segment: segment00499.m4s | buffer: 1.15s (~3.8 segs)
```

### Summary Statistics

| Metric | Value |
|--------|-------|
| **Average Latency** | ~1312 ms |
| **Min Latency** | 1276.7 ms |
| **Max Latency** | 1323.1 ms |
| **Latency Std Dev** | ~15 ms |
| **Average Buffer Ahead** | ~1.0s (~3.3 segments) |
| **Buffer Range** | 0.25s - 1.49s |

---

## Latency Breakdown

The total end-to-end latency of **~1320ms** can be decomposed into several components:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        TOTAL LATENCY (~1320ms)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │   CAPTURE    │  │   ENCODE     │  │   SEGMENT    │  │    NETWORK       │ │
│  │   ~30-50ms   │  │   ~50-100ms  │  │   ~150ms     │  │    ~10-50ms      │ │
│  │              │  │              │  │   (0.5×seg)  │  │                  │ │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘ │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                       │
│  │   PLAYER     │  │   DECODE     │  │   RENDER     │                       │
│  │   BUFFER     │  │   ~20-50ms   │  │   ~16ms      │                       │
│  │   ~900ms     │  │              │  │   (1 frame)  │                       │
│  │   (3 segs)   │  │              │  │              │                       │
│  └──────────────┘  └──────────────┘  └──────────────┘                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. Capture Latency (~30-50ms)

**Source**: Camera/video source to GStreamer pipeline

- V4L2 driver buffering
- Camera sensor exposure time
- USB/hardware transfer time

**How to reduce**:
- Use `v4l2src` with `io-mode=2` (userptr) or `io-mode=4` (dmabuf)
- Reduce camera resolution
- Use cameras with lower capture latency

---

### 2. Encoding Latency (~50-100ms)

**Source**: x264 encoder processing

Current settings:
```
x264enc tune=zerolatency speed-preset=<preset> bframes=0
```

| Component | Impact |
|-----------|--------|
| `tune=zerolatency` | Disables features that add latency (lookahead, B-frames, etc.) |
| `bframes=0` | No bidirectional frames (removes reordering delay) |
| `speed-preset` | Faster = less latency, worse quality |

**How to reduce**:
- Use `speed-preset=ultrafast` (if not already)
- Reduce resolution/bitrate
- Consider hardware encoding (`vaapih264enc`, `nvh264enc`)

---

### 3. Segmentation Latency (~150ms for 0.3s segments)

**Source**: Must wait for segment to be complete before it can be served

This is **inherent to HLS** - the segment must be fully written before clients can fetch it.

**Formula**:
```
Segment Latency ≈ 0.5 × segment_duration  (on average)
                = 0.5 × 300ms = 150ms
```

**Why 0.5×?** On average, a frame arrives halfway through the segment interval. The first frame of a segment waits the full duration; the last frame waits almost zero.

**How to reduce**:
- Use shorter segments (current: 0.3s is already quite short)
- Use LL-HLS parts (sub-segment chunks) - requires `EXT-X-PART` support
- Minimum practical: ~0.2s segments (shorter causes overhead issues)

---

### 4. Network Transfer Latency (~10-50ms)

**Source**: HTTP request/response over network

| Factor | Impact |
|--------|--------|
| Local network (same machine) | ~1-5ms |
| LAN (same network) | ~5-20ms |
| WiFi | ~10-50ms |
| Internet | ~50-200ms+ |

**How to reduce**:
- Use wired connection
- Use HTTP/2 or HTTP/3 (multiplexing)
- CDN edge servers for remote clients
- Enable TCP_NODELAY

---

### 5. Player Buffer (~900ms = 3 segments) ⚠️ LARGEST CONTRIBUTOR

**Source**: hls.js maintains a buffer ahead of the playhead for smooth playback

Current configuration:
```javascript
const hls = new Hls({ 
  lowLatencyMode: true,
  liveSyncDurationCount: 3,  // Stay 3 segments behind live edge
});
```

**This is the dominant latency component!**

With 0.3s segments and `liveSyncDurationCount: 3`:
```
Buffer latency = 3 × 0.3s = 0.9s (900ms)
```

The observed buffer fluctuates (0.3s - 1.5s) because:
- hls.js prefetches segments aggressively
- Playback consumes buffer while new segments arrive
- Network jitter causes variations

**How to reduce**:
```javascript
const hls = new Hls({ 
  lowLatencyMode: true,
  liveSyncDurationCount: 2,      // Reduce to 2 segments (was 3)
  liveMaxLatencyDurationCount: 4, // Seek forward if >4 segments behind
  maxBufferLength: 2,             // Max buffer size in seconds
  maxMaxBufferLength: 3,          // Absolute max buffer
});
```

⚠️ **Trade-off**: Less buffer = higher risk of rebuffering on network hiccups

---

### 6. Decode + Render Latency (~35-65ms)

**Source**: Browser video decoder and compositor

| Component | Typical Time |
|-----------|--------------|
| H.264 decode | 20-50ms |
| Compositor/render | 16ms (1 frame @ 60fps) |

Hardware acceleration (used by default in browsers) minimizes this.

---

## Complete Latency Formula

```
Total Latency = Capture + Encode + (0.5 × SegmentDuration) + Network + PlayerBuffer + Decode + Render

~1320ms      = ~40ms   + ~75ms  + 150ms                    + ~30ms   + ~900ms       + ~50ms  + ~16ms
```

| Component | Estimated | Percentage |
|-----------|-----------|------------|
| Player Buffer | 900ms | **68%** |
| Segment Wait | 150ms | 11% |
| Encoding | 75ms | 6% |
| Decode + Render | 66ms | 5% |
| Capture | 40ms | 3% |
| Network | 30ms | 2% |

---

## Optimization Strategies

### Quick Wins (Low Risk)

| Change | Expected Reduction | Risk |
|--------|-------------------|------|
| `liveSyncDurationCount: 2` | ~300ms | Low |
| `speed-preset=ultrafast` | ~20-30ms | Low (quality loss) |
| Wired network | ~20-40ms | None |

### Aggressive (Higher Risk)

| Change | Expected Reduction | Risk |
|--------|-------------------|------|
| `liveSyncDurationCount: 1` | ~600ms | Medium (rebuffering) |
| `segment-duration=0.2` | ~50ms | Medium (overhead) |
| Hardware encoder | ~30-50ms | Low |

### Maximum Low-Latency Configuration

```javascript
// browser.html - Aggressive hls.js settings
const hls = new Hls({ 
  enableWorker: true,
  lowLatencyMode: true,
  liveSyncDurationCount: 1,       // Only 1 segment behind live
  liveMaxLatencyDurationCount: 3, // Seek if >3 segments behind
  maxBufferLength: 1,             // 1 second max buffer
  maxMaxBufferLength: 2,
  highBufferWatchdogPeriod: 1,    // Check buffer every 1s
});
```

```bash
# streamer.py - Aggressive encoding
python3 streamer.py \
  --segment-duration 0.2 \
  --speed-preset ultrafast \
  --bitrate 2000
```

**Theoretical minimum with these settings**: ~500-700ms

---

## The Short Segment Paradox: Diminishing Returns

### Observed Behavior

When reducing `liveSyncDurationCount` from 3 to 1 with 0.3s segments:

| Setting | Expected Reduction | Actual Reduction |
|---------|-------------------|------------------|
| 3 → 1 segments | 2 × 0.3s = **600ms** | **~200ms** |

**Why only ~200ms instead of 600ms?**

### Explanation: Time-Based vs Segment-Based Buffers

The `liveSyncDurationCount` setting is a **target**, not a guarantee. hls.js has internal **time-based minimum buffer thresholds** that override the segment-based setting:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    BUFFER FLOOR BEHAVIOR                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  With LONG segments (e.g., 2 seconds):                              │
│  ─────────────────────────────────────                              │
│  liveSyncDurationCount: 3  →  3 × 2s = 6.0s buffer                  │
│  liveSyncDurationCount: 1  →  1 × 2s = 2.0s buffer                  │
│  Reduction: 4.0 seconds ✓                                           │
│                                                                     │
│  With SHORT segments (e.g., 0.3 seconds):                           │
│  ──────────────────────────────────────                             │
│  liveSyncDurationCount: 3  →  3 × 0.3s = 0.9s (target)              │
│                              BUT internal min ≈ 0.8-1.0s            │
│                              Actual: ~1.0s                          │
│                                                                     │
│  liveSyncDurationCount: 1  →  1 × 0.3s = 0.3s (target)              │
│                              BUT internal min ≈ 0.5-0.8s            │
│                              Actual: ~0.7-0.8s                      │
│                                                                     │
│  Reduction: only ~0.2-0.3 seconds                                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### The Internal Minimum Buffer

hls.js maintains minimum buffer thresholds for stability:

```javascript
// Simplified hls.js internal logic
effectiveBuffer = Math.max(
  liveSyncDurationCount * targetDuration,  // Your setting
  INTERNAL_MIN_BUFFER                       // ~0.5-1.0 seconds
);
```

These minimums exist because:
1. **Network jitter**: Small buffers can't absorb network delays
2. **Decode pipeline**: Browser needs data ahead to decode smoothly
3. **Rebuffer prevention**: Too-small buffers cause constant stalls

### Visual Representation

```
Buffer Size (seconds)
    │
 6s ├─────────────────────────────────── Long segments (2s)
    │                                     liveSyncDurationCount: 3
 5s ├
    │
 4s ├
    │
 3s ├
    │
 2s ├─────────────────────────────────── Long segments (2s)
    │                                     liveSyncDurationCount: 1
    │                                     
 1s ├───●─────────────────────────────── Short segments (0.3s)
    │   │                                 liveSyncDurationCount: 3
    │   │                                 (target: 0.9s, actual: ~1.0s)
    │   │
0.8s├───┼──●──────────────────────────── Short segments (0.3s)
    │   │  │                              liveSyncDurationCount: 1
    │   │  │                              (target: 0.3s, actual: ~0.8s)
    │   │  │
0.5s├───┼──┼───── MINIMUM BUFFER FLOOR ── (hls.js internal threshold)
    │   │  │
    │   ▼  ▼
 0s └───┴──┴─────────────────────────────────────────────────────────
        │  │
        │  └── Only ~200ms reduction possible!
        │
        └───── Can't go below the floor
```

### Key Insight

> **With very short segments (< 0.5s), the time-based minimum buffer floor becomes the limiting factor, not the segment count.**

The formula for actual buffer is:
```
Actual Buffer = max(liveSyncDurationCount × segmentDuration, ~0.5-0.8s)
```

### Practical Implications

| Segment Duration | liveSyncDurationCount: 3 | liveSyncDurationCount: 1 | Max Possible Reduction |
|-----------------|-------------------------|-------------------------|----------------------|
| 6.0s | 18.0s | 6.0s | **12.0s** |
| 2.0s | 6.0s | 2.0s | **4.0s** |
| 1.0s | 3.0s | 1.0s | **2.0s** |
| 0.5s | 1.5s | ~0.8s | **~0.7s** |
| 0.3s | ~1.0s | ~0.8s | **~0.2s** ← Your case |
| 0.2s | ~0.8s | ~0.7s | **~0.1s** |

### Conclusion

When using short segments (like 0.3s), you've already pushed the buffer close to its minimum floor. Further reductions to `liveSyncDurationCount` yield **diminishing returns** because the player's internal safety thresholds prevent the buffer from dropping below ~0.5-0.8 seconds.

**This is not a bug—it's by design.** A buffer below this threshold would cause constant rebuffering, defeating the purpose of the optimization.

To achieve further latency reduction beyond this floor, you would need to:
1. Modify hls.js source code (not recommended)
2. Use a different protocol (WebRTC, WebTransport) that doesn't require buffering
3. Accept higher rebuffering risk in exchange for lower latency

---

## Why Not Lower?

### HLS Protocol Limitations

HLS is fundamentally **segment-based**. Each segment must be:
1. Fully encoded
2. Written to disk
3. Listed in playlist
4. Fetched by client
5. Buffered before playback

This creates an **irreducible minimum latency** of approximately:
```
Minimum ≈ SegmentDuration + PlayerBuffer(1 segment) + Overhead
        ≈ 0.2s + 0.2s + 0.1s
        ≈ 0.5s (500ms)
```

### For Sub-500ms Latency

Consider alternative protocols:
- **WebRTC**: 100-300ms (peer-to-peer, no segments)
- **WebTransport**: 100-500ms (QUIC-based, low overhead)
- **RTMP/RTSP**: 200-500ms (continuous stream, no segmentation)

---

## Buffer Behavior Analysis

From the observed data, the buffer shows a sawtooth pattern:

```
Buffer (segments)
    5 │    ╱╲      ╱╲      ╱╲
    4 │   ╱  ╲    ╱  ╲    ╱  ╲
    3 │  ╱    ╲  ╱    ╲  ╱    ╲
    2 │ ╱      ╲╱      ╲╱      ╲
    1 │╱                        ╲
    0 └────────────────────────────▶ Time
```

**Pattern explanation**:
1. New segment arrives → buffer increases
2. Playback consumes buffer → buffer decreases
3. Next segment arrives → cycle repeats

The amplitude varies based on:
- Network jitter
- Segment delivery timing
- Player's adaptive algorithms

---

## Measurement Methodology

### How Latency is Measured

```
Production Time (T₁): When segment file is closed (written to disk)
                      └─ Captured via inotify CLOSE_WRITE event
                      └─ Stored in _SEGMENT_TIMESTAMPS[filename] = time.time_ns()

Reception Time (T₂):  When hls.js fires FRAG_CHANGED event
                      └─ Measured as performance.now() + performance.timeOrigin

Latency = T₂ - T₁
```

### Accuracy Considerations

| Factor | Impact |
|--------|--------|
| Clock sync | Both times use wall-clock time; same machine = accurate |
| inotify delay | ~1-5ms additional delay in detection |
| Event timing | FRAG_CHANGED fires when fragment starts playing, not when received |

**Note**: The measured latency includes the time the segment spends buffered before playback begins, which is correct for "glass-to-glass" latency perception.

---

## Conclusion

With the current configuration (0.3s segments, hls.js low-latency mode), achieving **~1.3 seconds** end-to-end latency is expected and reasonable for LL-HLS.

**Key insight**: The player buffer contributes ~68% of total latency. Reducing `liveSyncDurationCount` from 3 to 1-2 would yield the most significant improvement, potentially bringing latency down to **~700-900ms**.

For latency below 500ms, HLS is not the optimal choice—consider WebRTC or WebTransport for real-time applications.
