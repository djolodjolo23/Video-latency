# Short-Segment HLS vs Apple LL-HLS Specification

## Overview

This document compares our implementation approach with the official Apple Low-Latency HLS (LL-HLS) specification, explaining the differences, trade-offs, and rationale for our design decisions.

---

## Apple's LL-HLS Specification

Apple's [Low-Latency HLS specification](https://developer.apple.com/documentation/http-live-streaming/enabling-low-latency-http-live-streaming-ll-hls) defines a comprehensive set of extensions to enable low-latency streaming while maintaining CDN scalability. The specification was introduced in HTTP Live Streaming 2nd Edition, revision 7.

### Core Features

| Feature | Description |
|---------|-------------|
| **Partial Segments** (`EXT-X-PART`) | Divide 6s parent segments into ~200ms chunks |
| **Blocking Playlist Reload** | Server holds request until new content is available |
| **Preload Hints** (`EXT-X-PRELOAD-HINT`) | Tell client what resource is coming next |
| **Delta Updates** (`EXT-X-SKIP`) | Send only playlist changes, not full playlist |
| **Rendition Reports** (`EXT-X-RENDITION-REPORT`) | Cross-rendition metadata for fast ABR switching |
| **Server Control** (`EXT-X-SERVER-CONTROL`) | Advertise server capabilities |
| **Delivery Directives** | Query params (`_HLS_msn`, `_HLS_part`) for blocking requests |

### How Apple LL-HLS Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         APPLE LL-HLS ARCHITECTURE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Parent Segment (6 seconds) divided into Partial Segments                  │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Part 0 │ Part 1 │ Part 2 │ Part 3 │ ... │ Part 28 │ Part 29           │ │
│  │ 200ms  │ 200ms  │ 200ms  │ 200ms  │     │ 200ms   │ 200ms             │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  Example Playlist at live edge:                                             │
│  ┌──────────────────────────────────────────────────────────────┐           │
│  │ #EXTM3U                                                      │           │
│  │ #EXT-X-TARGETDURATION:6                                      │           │
│  │ #EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,                  │           │
│  │   PART-HOLD-BACK=1.0,CAN-SKIP-UNTIL=36.0                     │           │
│  │ #EXT-X-PART-INF:PART-TARGET=0.2                              │           │
│  │ #EXT-X-MAP:URI="init.mp4"                                    │           │
│  │ ...                                                          │           │
│  │ #EXTINF:6.0,                                                 │           │
│  │ segment100.m4s                                               │           │
│  │ #EXT-X-PART:DURATION=0.2,URI="segment101.0.m4s"              │           │
│  │ #EXT-X-PART:DURATION=0.2,URI="segment101.1.m4s"              │           │
│  │ #EXT-X-PART:DURATION=0.2,URI="segment101.2.m4s"              │           │
│  │ #EXT-X-PRELOAD-HINT:TYPE=PART,URI="segment101.3.m4s"         │           │
│  └──────────────────────────────────────────────────────────────┘           │
│                                                                             │
│  Client-Server Interaction (BLOCKING):                                      │
│  ─────────────────────────────────────                                      │
│                                                                             │
│  Client: GET /live.m3u8?_HLS_msn=101&_HLS_part=3                            │
│       │                                                                     │
│       └──▶ Server HOLDS connection until part 3 is ready                    │
│            │                                                                │
│            └──▶ Immediate response when available                           │
│                 (NO POLLING - client notified instantly!)                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key LL-HLS Playlist Tags

| Tag | Purpose |
|-----|---------|
| `EXT-X-SERVER-CONTROL` | Declares server capabilities: `CAN-BLOCK-RELOAD`, `PART-HOLD-BACK`, `CAN-SKIP-UNTIL` |
| `EXT-X-PART-INF` | Declares the target duration of partial segments |
| `EXT-X-PART` | Defines a partial segment within a parent segment |
| `EXT-X-PRELOAD-HINT` | Hints at upcoming resources for preloading |
| `EXT-X-SKIP` | Used in delta updates to skip unchanged playlist content |
| `EXT-X-RENDITION-REPORT` | Provides info about other renditions for fast ABR switching |

### Delivery Directives

| Directive | Purpose |
|-----------|---------|
| `_HLS_msn=<M>` | Request playlist with media sequence number M or later |
| `_HLS_part=<N>` | Request playlist with part N of segment M or later |
| `_HLS_skip=YES` | Request a delta update instead of full playlist |

---

## Our Implementation: Short-Segment HLS

Instead of implementing full LL-HLS, we use a simplified **short-segment approach** that achieves low latency through reduced segment duration.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      SHORT-SEGMENT HLS (OUR APPROACH)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Independent short segments (0.3 seconds each)                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │ seg 001  │ │ seg 002  │ │ seg 003  │ │ seg 004  │ │ seg 005  │          │
│  │  0.3s    │ │  0.3s    │ │  0.3s    │ │  0.3s    │ │  0.3s    │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│                                                                             │
│  Simple Playlist:                                                           │
│  ┌──────────────────────────────────────────────────────────────┐           │
│  │ #EXTM3U                                                      │           │
│  │ #EXT-X-VERSION:9                                             │           │
│  │ #EXT-X-TARGETDURATION:1                                      │           │
│  │ #EXT-X-MAP:URI="init.mp4"                                    │           │
│  │ #EXT-X-MEDIA-SEQUENCE:100                                    │           │
│  │ #EXTINF:0.3,                                                 │           │
│  │ segment00100.m4s                                             │           │
│  │ #EXTINF:0.3,                                                 │           │
│  │ segment00101.m4s                                             │           │
│  │ #EXTINF:0.3,                                                 │           │
│  │ segment00102.m4s                                             │           │
│  │ ...                                                          │           │
│  └──────────────────────────────────────────────────────────────┘           │
│                                                                             │
│  Client-Server Interaction (POLLING):                                       │
│  ─────────────────────────────────────                                      │
│                                                                             │
│  Client: GET /live.m3u8  →  Response                                        │
│          [wait ~300ms]                                                      │
│  Client: GET /live.m3u8  →  Response (maybe new segment?)                   │
│          [wait ~300ms]                                                      │
│  Client: GET /live.m3u8  →  Response                                        │
│          ...                                                                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Feature Comparison

| Feature | Apple LL-HLS | Our Approach |
|---------|--------------|--------------|
| Segment duration | 6s parent + 200ms parts | 300ms full segments |
| Playlist updates | Blocking reload | Client polling (~300ms intervals) |
| `EXT-X-PART` | ✅ Required | ❌ Not used |
| `EXT-X-PRELOAD-HINT` | ✅ Required | ❌ Not used |
| `EXT-X-SERVER-CONTROL` | ✅ Required | ❌ Not used |
| Blocking requests | ✅ `_HLS_msn`, `_HLS_part` | ❌ Standard HTTP GET |
| Delta updates | ✅ `EXT-X-SKIP` | ❌ Full playlist each time |
| Rendition reports | ✅ `EXT-X-RENDITION-REPORT` | ❌ Not used |
| CDN optimization | ✅ Designed for global CDN | ⚠️ Works, not optimized |
| Player compatibility | Requires LL-HLS aware player | Any HLS player |

---

## Latency Comparison

| Metric | Apple LL-HLS | Our Approach |
|--------|--------------|--------------|
| Typical latency | 2-5 seconds | ~1.3 seconds |
| Best-case latency | ~1 second | ~1 second |
| Polling overhead | None (blocking) | Up to 300ms per segment |
| CDN tune-in time | Optimized | Standard |

**Note**: Our approach actually achieves comparable or better latency than many LL-HLS deployments because we use very short segments (0.3s) and run on a local network without CDN hops.

---

## Trade-offs Analysis

### Advantages of Our Approach

| Advantage | Explanation |
|-----------|-------------|
| **Simplicity** | Standard HLS playlist generation, no special server logic |
| **Implementation time** | Can be implemented in hours vs. days/weeks |
| **Server requirements** | Any HTTP server (no blocking request support needed) |
| **Player compatibility** | Works with any HLS player, not just LL-HLS aware ones |
| **Debugging** | Standard tools work, easier to troubleshoot |
| **Latency achieved** | ~1.3s is sufficient for our use case |

### Advantages of Apple LL-HLS

| Advantage | Explanation |
|-----------|-------------|
| **CDN scalability** | Designed for global CDN deployment with caching |
| **Bandwidth efficiency** | Delta updates reduce playlist transfer size |
| **Notification efficiency** | Blocking reloads eliminate polling waste |
| **Standardization** | Industry-standard specification |
| **ABR performance** | Rendition reports enable fast quality switching |
| **Part caching** | Parts can be cached while parent segment builds |

---

## Why We Chose Short-Segment HLS

### 1. Time Constraints

Implementing full LL-HLS requires:
- Custom HTTP server with blocking request support
- Partial segment generation and management
- Playlist generation with all LL-HLS tags
- Delta update logic
- Preload hint generation
- Testing and validation

**Estimated implementation time**:
- Full LL-HLS: 2-4 weeks
- Short-segment HLS: 1-2 days ✓

### 2. Complexity

| Component | LL-HLS Complexity | Short-Segment Complexity |
|-----------|-------------------|-------------------------|
| Server | High (blocking, state management) | Low (stateless HTTP) |
| Playlist generation | High (parts, hints, reports) | Low (standard tags) |
| Segment management | Medium (parts + parent cleanup) | Low (simple rotation) |
| Client requirements | LL-HLS aware player | Any HLS player |

### 3. Sufficient Latency

Our achieved latency of **~1.3 seconds** meets our requirements:
- Acceptable for live monitoring applications
- Within range of broadcast television latency
- Good enough for non-interactive use cases

For truly interactive applications (< 500ms), neither approach is suitable—WebRTC or WebTransport would be required.

### 4. Local Network Deployment

Our use case is local network streaming, not global CDN:
- No CDN caching benefits needed
- Low network latency already
- No need for delta updates (bandwidth not constrained)
- No geographic distribution

---

## Implementation Complexity Breakdown

### Full LL-HLS Server Requirements

```python
# Pseudo-code for LL-HLS server complexity

class LLHLSServer:
    def __init__(self):
        self.pending_requests = {}  # Track blocking requests
        self.parts = {}             # Track partial segments
        self.parent_segments = {}   # Track parent segments
        
    def handle_playlist_request(self, request):
        msn = request.query.get('_HLS_msn')
        part = request.query.get('_HLS_part')
        skip = request.query.get('_HLS_skip')
        
        if msn and part:
            # BLOCKING: Hold request until part is available
            if not self.is_part_available(msn, part):
                future = self.create_pending_request(msn, part)
                return await future  # Block here!
        
        if skip:
            return self.generate_delta_playlist()
        
        return self.generate_full_playlist()
    
    def on_new_part_available(self, msn, part):
        # Wake up all pending requests for this part
        for request in self.pending_requests.get((msn, part), []):
            request.complete(self.generate_playlist())
        
        # Update preload hint
        self.update_preload_hint(msn, part + 1)
        
        # Generate rendition reports
        self.update_rendition_reports()
    
    def generate_playlist(self):
        # Must include:
        # - EXT-X-SERVER-CONTROL
        # - EXT-X-PART-INF  
        # - EXT-X-PART for each partial segment
        # - EXT-X-PRELOAD-HINT
        # - EXT-X-RENDITION-REPORT (if multi-rendition)
        # - Proper cleanup of old parts
        ...
```

### Our Simple Approach

```python
# Our actual implementation (simplified)

class SimpleHLSServer:
    def handle_request(self, path):
        if path.endswith('.m3u8'):
            return self.read_playlist_file()
        elif path.endswith('.m4s'):
            return self.read_segment_file(path)
        elif path == '/timestamps.json':
            return json.dumps(self.segment_timestamps)
```

---

## GStreamer Plugin Investigation

We investigated whether full Apple LL-HLS implementation was feasible with our GStreamer-based pipeline.

### Available Plugins

| Plugin | Element | Purpose |
|--------|---------|---------|
| `hlssink3` | `hlscmafsink` | HLS CMAF sink (used in our implementation) |
| `isobmff` | `cmafmux` | Low-level CMAF muxer with chunk support |

### hlscmafsink Analysis

```
$ gst-inspect-1.0 hlscmafsink

Plugin: hlssink3 (version 0.15.0-alpha.1)
Key Properties:
  location          : "segment%05d.m4s"     (fragment file location)
  init-location     : "init%05d.mp4"        (init segment location)  
  target-duration   : 15 seconds            (segment duration)
  latency           : 7500000000 ns         (pipeline latency)
```

**Finding**: `hlscmafsink` produces **one complete `.m4s` file per fragment**. It does NOT generate separate part files required for LL-HLS.

### cmafmux Analysis

The underlying `cmafmux` element (from `isobmff` plugin) has promising chunk support:

```
$ gst-inspect-1.0 cmafmux

chunk-duration    : Duration for each FMP4 chunk (nanoseconds)
                    Default: 18446744073709551615 (disabled)
                    
chunk-mode        : Mode to control chunking
                    (0): none      - None
                    (1): duration  - Duration-based chunks
                    (2): keyframe  - Keyframe-based chunks
                    
fragment-duration : Duration for each parent fragment (nanoseconds)
                    Default: 10000000000 (10 seconds)
                    
emit-signals      : Send signals when data is available
                    Default: false
```

**Finding**: `cmafmux` DOES support CMAF chunks (equivalent to LL-HLS parts) via the `chunk-duration` and `chunk-mode` properties.

### The Gap

While `cmafmux` can produce chunked output internally, `hlscmafsink` does not expose these chunks as separate files:

```
┌─────────────────────────────────────────────────────────────────┐
│                     GStreamer LL-HLS Status                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   hlscmafsink wraps cmafmux but:                               │
│   ┌───────────────────────────────────────────────────────┐    │
│   │  cmafmux                        hlscmafsink           │    │
│   │  ┌──────────┐                   ┌──────────────────┐  │    │
│   │  │ chunk 0  │──┐                │                  │  │    │
│   │  │ chunk 1  │──┼── combined ───▶│  segment001.m4s  │  │    │
│   │  │ chunk 2  │──┘   internally   │  (single file)   │  │    │
│   │  └──────────┘                   └──────────────────┘  │    │
│   └───────────────────────────────────────────────────────┘    │
│                                                                 │
│   LL-HLS REQUIRES:              WHAT WE GET:                   │
│   segment001.0.m4s (part 0)     segment00001.m4s (complete)    │
│   segment001.1.m4s (part 1)     segment00002.m4s (complete)    │
│   segment001.2.m4s (part 2)     ...                            │
│   segment001.m4s   (parent)                                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Theoretical Path to Full LL-HLS

Based on community guidance, full LL-HLS IS technically achievable by:

1. **Using `cmafmux` directly** (bypass `hlscmafsink`)
2. **Configuring chunk output**:
   ```python
   cmafmux.set_property("chunk-duration", 200000000)  # 200ms
   cmafmux.set_property("chunk-mode", 1)  # duration-based
   cmafmux.set_property("emit-signals", True)
   ```
3. **Connecting to `appsink`** to receive chunk data
4. **Writing chunks to separate files** manually
5. **Generating HLS manifest yourself** with `EXT-X-PART` tags
6. **Implementing async HTTP server** for blocking playlist requests

### Implementation Effort Estimate

| Component | Effort | Notes |
|-----------|--------|-------|
| Replace hlscmafsink with cmafmux + appsink | 1-2 days | Pipeline restructuring |
| Parse CMAF chunks from muxer output | 1-2 days | Understand moof/mdat structure |
| Write chunks as individual part files | 1 day | File management |
| Manual HLS playlist generation | 1-2 days | EXT-X-PART, preload hints |
| Async HTTP server (aiohttp) | 1 day | Blocking requests |
| Testing & edge cases | 2-3 days | Timing, cleanup, error handling |
| **Total** | **7-12 days** | |

### Why We Chose Not to Implement Full LL-HLS

| Factor | Assessment |
|--------|------------|
| **Time constraint** | Project timeline does not allow 7-12 days additional development |
| **Current latency** | ~1.3 seconds is acceptable for our use case |
| **Potential improvement** | Full LL-HLS would save ~200-400ms (modest gain) |
| **Complexity** | Requires understanding CMAF internals, custom muxing |
| **Maintenance burden** | Custom implementation harder to maintain than using hlscmafsink |
| **Player requirements** | Current approach works with any HLS player |

### GStreamer LL-HLS Support Status

Note: GStreamer 1.24+ added LL-HLS support to `hlsdemux2` (the **playback** side):

> "Adaptive Streaming improvements and Low-Latency HLS (LL-HLS) support - hlsdemux2 now supports Low-Latency HLS (LL-HLS)"

However, this is for **consuming** LL-HLS streams, not **producing** them. The server-side tooling (`hlscmafsink`) does not yet have integrated LL-HLS part generation.

---

## Conclusion

Our short-segment HLS approach is a pragmatic choice that:

1. **Achieves acceptable latency** (~1.3 seconds) for our use case
2. **Minimizes implementation complexity** by using standard HLS
3. **Reduces development time** from weeks to days
4. **Works with any HLS player** without special requirements
5. **Is easier to debug and maintain** with standard tools

### Why Short-Segment HLS Works

| Aspect | Explanation |
|--------|-------------|
| **Latency** | 0.3s segments achieve ~1.3s end-to-end latency, comparable to LL-HLS |
| **Simplicity** | Uses `hlscmafsink` out-of-the-box, no custom code |
| **Compatibility** | Standard HLS tags work with all players |
| **Reliability** | Proven GStreamer pipeline, minimal moving parts |

### When to Consider Full LL-HLS

Full LL-HLS implementation would be justified if:
- **CDN deployment** is required (blocking requests reduce edge load)
- **Every millisecond matters** (broadcast, sports, betting)
- **Dedicated development time** is available (2+ weeks)
- **LL-HLS player compatibility** is specifically required

### Future Possibility

If GStreamer's `hlscmafsink` adds native LL-HLS part generation in the future, migration would be straightforward. The community has indicated this is technically possible using `cmafmux` with manual manifest generation.

For our current use case (local network streaming with ~1-2s latency tolerance), short-segment HLS is the right engineering trade-off.

