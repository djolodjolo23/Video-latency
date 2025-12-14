# Low-Latency HLS Streaming: Complete Technical Guide

## Table of Contents

1. [Introduction to HLS](#1-introduction-to-hls)
2. [Why Traditional HLS Has High Latency](#2-why-traditional-hls-has-high-latency)
3. [Low-Latency HLS (LL-HLS) Architecture](#3-low-latency-hls-ll-hls-architecture)
4. [CMAF: The Foundation of LL-HLS](#4-cmaf-the-foundation-of-ll-hls)
5. [Our Streaming Pipeline](#5-our-streaming-pipeline)
6. [Latency Optimization Techniques](#6-latency-optimization-techniques)
7. [Configuration Reference](#7-configuration-reference)
8. [Latency Measurement System](#8-latency-measurement-system)

---

## 1. Introduction to HLS

HTTP Live Streaming (HLS) is Apple's adaptive bitrate streaming protocol that delivers video content over standard HTTP. It works by:

1. **Segmenting** video into small chunks (typically 6-10 seconds)
2. **Creating a playlist** (`.m3u8` file) that lists available segments
3. **Serving** both playlist and segments via standard HTTP servers
4. **Allowing clients** to fetch and play segments sequentially

```
┌─────────────────────────────────────────────────────────────────┐
│                     HLS Streaming Flow                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  Camera  │───▶│ Encoder  │───▶│ Segmenter│───▶│  HTTP    │  │
│  │          │    │          │    │          │    │  Server  │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│                                                       │         │
│                                                       ▼         │
│                                              ┌──────────────┐   │
│                                              │   Playlist   │   │
│                                              │  (live.m3u8) │   │
│                                              ├──────────────┤   │
│                                              │ segment1.ts  │   │
│                                              │ segment2.ts  │   │
│                                              │ segment3.ts  │   │
│                                              └──────────────┘   │
│                                                       │         │
│                                                       ▼         │
│                                              ┌──────────────┐   │
│                                              │    Client    │   │
│                                              │   (Player)   │   │
│                                              └──────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Why HLS is Popular

- Works over standard HTTP (port 80/443)
- Passes through firewalls and CDNs
- Adaptive bitrate support
- Wide player compatibility
- No special server software required

---

## 2. Why Traditional HLS Has High Latency

Traditional HLS was designed for reliability, not low latency. Several factors contribute to high latency:

### 2.1 Segment Duration

Traditional HLS uses 6-10 second segments. A client must wait for:
- Current segment to be **fully encoded**
- Segment to be **uploaded to server**
- Client to **download** the segment
- Segment to be **decoded and played**

```
Timeline showing why 6-second segments cause delay:

Encoder:     |====== Seg 1 ======|====== Seg 2 ======|====== Seg 3 ======|
                                 ↓
Server:                          |====== Seg 1 ======|
                                                     ↓
Client:                                              |====== Seg 1 ======|
                                                                         ↓
Playback:                                                                |► Playing

Total delay: ~18-24 seconds from capture to playback
```

### 2.2 Buffer Requirements

HLS clients typically buffer 2-3 segments before starting playback:

| Segment Size | Buffer (3 segments) | Minimum Latency |
|--------------|---------------------|-----------------|
| 10 seconds   | 30 seconds          | 30+ seconds     |
| 6 seconds    | 18 seconds          | 18+ seconds     |
| 2 seconds    | 6 seconds           | 6+ seconds      |

### 2.3 Playlist Polling

Clients must poll the playlist file to discover new segments. The polling interval adds delay:

```
Playlist update: |----------|----------|----------|
Client poll:           ^         ^         ^
                       |         |         |
                  Segment      Segment   Segment
                  discovered   discovered discovered
                  (delayed)    (delayed)  (delayed)
```

### 2.4 MPEG-TS Container Overhead

Traditional HLS uses MPEG-TS (`.ts`) container format:
- Each segment is self-contained
- Contains redundant codec information
- Cannot be easily split into smaller parts
- Larger file sizes

---

## 3. Low-Latency HLS (LL-HLS) Architecture

Low-Latency HLS addresses these issues through several mechanisms:

### 3.1 Shorter Segments

LL-HLS uses segment durations of 0.3-2 seconds instead of 6-10 seconds:

```
Traditional HLS:  |========== 6 sec ==========|========== 6 sec ==========|

Low-Latency HLS:  |= 0.5s =|= 0.5s =|= 0.5s =|= 0.5s =|= 0.5s =|= 0.5s =|
```

### 3.2 CMAF Container Format

LL-HLS uses Common Media Application Format (CMAF) instead of MPEG-TS:
- Fragmented MP4 structure
- Separate initialization segment (codec info)
- Smaller media segments (only video/audio data)
- Enables partial segment delivery

### 3.3 Reduced Buffering

With shorter segments, clients can buffer fewer seconds while maintaining stability:

| Segment Size | Buffer (3 segments) | Minimum Latency |
|--------------|---------------------|-----------------|
| 0.5 seconds  | 1.5 seconds         | 2-3 seconds     |
| 0.3 seconds  | 0.9 seconds         | 1-2 seconds     |

### 3.4 Latency Comparison

```
┌────────────────────────────────────────────────────────────────────┐
│                    Latency Comparison                              │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Traditional HLS (6s segments):                                    │
│  |◀──────────────────── 18-30 seconds ────────────────────▶|      │
│  [Encode][Upload][Buffer 3 seg][Decode][Display]                   │
│                                                                    │
│  Low-Latency HLS (0.5s segments):                                  │
│  |◀──── 2-3 seconds ────▶|                                        │
│  [Enc][Up][Buf][Dec][Display]                                      │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. CMAF: The Foundation of LL-HLS

### 4.1 What is CMAF?

Common Media Application Format (CMAF) is a container format based on fragmented MP4 (fMP4). It separates codec configuration from media data:

```
┌─────────────────────────────────────────────────────────────────┐
│                    CMAF Structure                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Initialization Segment (init.mp4) - Downloaded once            │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ ftyp │ moov (codec config, track info, SPS/PPS)         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                           ↓                                      │
│                    Referenced by                                 │
│                           ↓                                      │
│  Media Segments (.m4s) - Downloaded continuously                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │styp│moof│ │styp│moof│ │styp│moof│ │styp│moof│            │
│  │    │mdat│ │    │mdat│ │    │mdat│ │    │mdat│            │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│   Segment 1    Segment 2    Segment 3    Segment 4              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Initialization Segment Structure

The initialization segment (`init.mp4`) contains all codec configuration:

```
init.mp4 (~800 bytes)
├── ftyp (File Type Box)
│   └── brands: iso5, avc1, iso6, mp41
│
└── moov (Movie Box)
    ├── mvhd (Movie Header)
    │   └── timescale, duration
    │
    ├── trak (Track Box)
    │   ├── tkhd (Track Header)
    │   │   └── track_id, width, height
    │   │
    │   └── mdia (Media Box)
    │       ├── mdhd (Media Header)
    │       │   └── timescale, language
    │       │
    │       ├── hdlr (Handler)
    │       │   └── handler_type: "vide"
    │       │
    │       └── minf (Media Information)
    │           └── stbl (Sample Table)
    │               └── stsd (Sample Description)
    │                   └── avc1 (H.264 Codec)
    │                       └── avcC (Decoder Config)
    │                           ├── SPS (Sequence Parameter Set)
    │                           └── PPS (Picture Parameter Set)
    │
    └── mvex (Movie Extends)
        └── trex (Track Extends)
            └── default sample settings
```

### 4.3 Media Segment Structure

Each media segment (`.m4s`) contains only video frames:

```
segment00001.m4s (~15-50 KB per 0.5s)
├── styp (Segment Type Box)
│   └── brands: msdh, msix
│
├── moof (Movie Fragment Box)
│   ├── mfhd (Movie Fragment Header)
│   │   └── sequence_number: 1
│   │
│   └── traf (Track Fragment Box)
│       ├── tfhd (Track Fragment Header)
│       │   └── track_id, base_data_offset
│       │
│       ├── tfdt (Track Fragment Decode Time)
│       │   └── base_media_decode_time
│       │
│       └── trun (Track Run Box)
│           └── sample_count, sample_sizes,
│               sample_durations, sample_flags
│
└── mdat (Media Data Box)
    └── H.264 NAL Units
        ├── IDR Frame (keyframe)
        ├── P-Frame
        ├── P-Frame
        └── ...
```

### 4.4 CMAF vs MPEG-TS Comparison

| Feature | MPEG-TS (.ts) | CMAF (.m4s) |
|---------|---------------|-------------|
| Codec info | In every segment | Separate init segment |
| Overhead | Higher (188-byte packets) | Lower (no packet overhead) |
| Fragmentation | Fixed packet size | Variable fragment size |
| Partial delivery | Difficult | Native support |
| File size | Larger | Smaller |
| Seeking | Complex | Simple (moof index) |

---

## 5. Our Streaming Pipeline

### 5.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        LL-HLS Streaming Pipeline                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────┐   ┌───────────┐   ┌─────────────┐   ┌──────────────────┐   │
│  │ v4l2src │──▶│ videorate │──▶│ timeoverlay │──▶│    x264enc       │   │
│  │ (camera)│   │ (30 fps)  │   │ (timestamp) │   │ (H.264 encoder)  │   │
│  └─────────┘   └───────────┘   └─────────────┘   └──────────────────┘   │
│                                                            │             │
│                                                            ▼             │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                        hlscmafsink                                │   │
│  │  ┌─────────────────────────────────────────────────────────────┐ │   │
│  │  │                         cmafmux                              │ │   │
│  │  │  • Fragments video into CMAF segments                        │ │   │
│  │  │  • Creates init segment with codec config                    │ │   │
│  │  │  • fragment-duration controls segment length                 │ │   │
│  │  └─────────────────────────────────────────────────────────────┘ │   │
│  │                              │                                    │   │
│  │  ┌─────────────────────────────────────────────────────────────┐ │   │
│  │  │                     Playlist Generator                       │ │   │
│  │  │  • Creates/updates live.m3u8                                 │ │   │
│  │  │  • Manages segment references                                │ │   │
│  │  │  • Handles segment rotation (max-files)                      │ │   │
│  │  └─────────────────────────────────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                      │                                   │
│                                      ▼                                   │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                         Output Directory                          │   │
│  │                                                                   │   │
│  │   init00000.mp4    live.m3u8    segment00001.m4s                 │   │
│  │   (codec config)   (playlist)   segment00002.m4s                 │   │
│  │                                 segment00003.m4s                 │   │
│  │                                 ...                              │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                      │                                   │
│                                      ▼                                   │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                         HTTP Server                               │   │
│  │                                                                   │   │
│  │   • Serves files on port 8080                                    │   │
│  │   • CORS headers for cross-origin playback                       │   │
│  │   • /timestamps.json endpoint for latency measurement            │   │
│  │                                                                   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Pipeline Components

#### Video Source (`v4l2src`)

Captures raw video from the camera:
```
v4l2src device=/dev/video0
```

#### Frame Rate Control (`videorate`)

Ensures consistent frame rate:
```
videorate ! video/x-raw,framerate=30/1
```

#### Timestamp Overlay (`timeoverlay`)

Burns current time into each frame for latency measurement:
```
timeoverlay color=0xffff0000 valignment=top halignment=left
```

#### H.264 Encoder (`x264enc`)

Encodes raw video to H.264 with low-latency settings:
```
x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 
        key-int-max=15 sliced-threads=true bframes=0
```

#### H.264 Parser (`h264parse`)

Ensures proper stream formatting for CMAF:
```
h264parse ! video/x-h264,stream-format=avc,alignment=au
```

#### CMAF HLS Sink (`hlscmafsink`)

Produces CMAF segments and HLS playlist:
```
hlscmafsink location=segment%05d.m4s 
            init-location=init%05d.mp4
            playlist-location=live.m3u8
            target-duration=1
```

Contains internal `cmafmux` element with `fragment-duration` property.

### 5.3 Complete GStreamer Pipeline

```
v4l2src device=/dev/video0 ! 
queue leaky=downstream max-size-buffers=1 ! 
videoconvert ! 
timeoverlay color=0xffff0000 valignment=top halignment=left ! 
videoscale ! 
videorate ! 
video/x-raw,width=1280,height=720,framerate=30/1 ! 
x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 
        key-int-max=15 sliced-threads=true bframes=0 ! 
h264parse ! 
video/x-h264,stream-format=avc,alignment=au ! 
hlscmafsink name=hlssink 
            location=output/segment%05d.m4s 
            init-location=output/init%05d.mp4 
            playlist-location=output/live.m3u8 
            target-duration=1 
            latency=500000000
```

---

## 6. Latency Optimization Techniques

### 6.1 Segment Duration

The most impactful factor. Controlled via the internal `cmafmux` element:

```python
# Access internal muxer via GstChildProxy interface
muxer = hlssink.get_child_by_name("muxer")
muxer.set_property("fragment-duration", 500_000_000)  # 0.5 seconds in nanoseconds
```

| Segment Duration | fragment-duration (ns) | Expected Latency |
|------------------|------------------------|------------------|
| 2.0 seconds      | 2,000,000,000          | 6-8 seconds      |
| 1.0 second       | 1,000,000,000          | 3-5 seconds      |
| 0.5 seconds      | 500,000,000            | 2-3 seconds      |
| 0.3 seconds      | 300,000,000            | 1-2 seconds      |

### 6.2 Keyframe Interval

Segments can only begin at keyframes (I-frames). The keyframe interval must match or be smaller than the segment duration:

```
key-int-max = framerate × segment_duration

Example for 0.5s segments at 30fps:
key-int-max = 30 × 0.5 = 15
```

```python
x264enc key-int-max=15  # Keyframe every 15 frames (0.5s at 30fps)
```

**Why this matters:**

```
If key-int-max is too large:

Frames:    |I|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|P|I|
                                                                      ↑
                                                          Next keyframe (1 second)

Desired segment: |◀── 0.5s ──▶|
Actual segment:  |◀─────────────────── 1.0s ───────────────────▶|
                 (waits for next keyframe)
```

### 6.3 Encoder Settings

#### Zero Latency Tuning

```python
x264enc tune=zerolatency
```

Disables:
- Frame reordering
- Lookahead
- Encoder buffering
- B-frame analysis

#### Speed Preset

```python
x264enc speed-preset=ultrafast  # or: superfast, veryfast, faster, fast
```

| Preset     | Encoding Speed | Quality | Latency Impact |
|------------|----------------|---------|----------------|
| ultrafast  | Fastest        | Lowest  | Minimal delay  |
| superfast  | Very fast      | Low     | Very low delay |
| veryfast   | Fast           | Medium  | Low delay      |
| medium     | Balanced       | Good    | Some delay     |
| slow       | Slow           | High    | High delay     |

#### No B-Frames

```python
x264enc bframes=0
```

B-frames require future frames for encoding, adding latency:

```
Without B-frames:    I → P → P → P → P → I
                     (encode immediately)

With B-frames:       I → B → B → P → B → B → P
                         ↑       ↑
                         │       └── Needs future P-frame
                         └── Needs future P-frame
                     (must buffer future frames)
```

### 6.4 Pipeline Buffering

Minimize internal queues:

```python
queue leaky=downstream max-size-buffers=1
```

- **`leaky=downstream`**: Drop old frames if processing falls behind
- **`max-size-buffers=1`**: Only buffer 1 frame in the queue

### 6.5 HLS Sink Latency

Control internal buffering in hlscmafsink:

```python
hlscmafsink latency=500000000  # 500ms in nanoseconds
```

Lower values = less buffering = lower latency (but may cause issues with slow encoders)

### 6.6 Summary of Latency Factors

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Latency Contribution Breakdown                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Total Latency = Capture + Encode + Segment + Network + Buffer + Decode │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │ Component        │ Traditional HLS │ LL-HLS (Optimized)          │   │
│  ├──────────────────┼─────────────────┼─────────────────────────────┤   │
│  │ Capture          │ ~33ms (1 frame) │ ~33ms (1 frame)             │   │
│  │ Encode           │ 100-500ms       │ 20-50ms (zerolatency)       │   │
│  │ Segment wait     │ 6000ms (6s seg) │ 500ms (0.5s seg)            │   │
│  │ Network          │ ~50-100ms       │ ~50-100ms                   │   │
│  │ Client buffer    │ 18000ms (3 seg) │ 1500ms (3 seg)              │   │
│  │ Decode           │ ~30ms           │ ~30ms                       │   │
│  ├──────────────────┼─────────────────┼─────────────────────────────┤   │
│  │ TOTAL            │ ~24-25 seconds  │ ~2-3 seconds                │   │
│  └──────────────────┴─────────────────┴─────────────────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Configuration Reference

### 7.1 Command Line Options

```bash
python3 streamer.py [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--device` | `/dev/video0` | Video capture device |
| `--width` | 1280 | Video width |
| `--height` | 720 | Video height |
| `--framerate` | 30 | Frames per second |
| `--bitrate` | 2000 | Video bitrate (kbps) |
| `--segment-duration` | 1.0 | Segment duration (seconds) |
| `--key-int-max` | 30 | Maximum keyframe interval |
| `--speed-preset` | ultrafast | x264 encoding preset |
| `--http-port` | 8080 | HTTP server port |
| `--output-dir` | ./output | Output directory |
| `--max-files` | 10 | Max segments to keep |

### 7.2 Configuration Profiles

#### Ultra Low Latency (~1-2 seconds)

```bash
python3 streamer.py \
  --segment-duration 0.3 \
  --key-int-max 9 \
  --speed-preset ultrafast \
  --bitrate 1500
```

#### Low Latency (~2-3 seconds)

```bash
python3 streamer.py \
  --segment-duration 0.5 \
  --key-int-max 15 \
  --speed-preset ultrafast \
  --bitrate 2000
```

#### Balanced (~3-5 seconds)

```bash
python3 streamer.py \
  --segment-duration 1.0 \
  --key-int-max 30 \
  --speed-preset veryfast \
  --bitrate 3000
```

#### Quality Priority (~5-8 seconds)

```bash
python3 streamer.py \
  --segment-duration 2.0 \
  --key-int-max 60 \
  --speed-preset medium \
  --bitrate 5000
```

### 7.3 Playlist Output

Example `live.m3u8` with 0.5-second segments:

```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:47
#EXT-X-MAP:URI="init00000.mp4"
#EXTINF:0.5,
segment00046.m4s
#EXTINF:0.5,
segment00047.m4s
#EXTINF:0.5,
segment00048.m4s
#EXTINF:0.5,
segment00049.m4s
#EXTINF:0.5,
segment00050.m4s
```

Key HLS Tags:
- `#EXT-X-VERSION:7` - HLS version supporting fMP4/CMAF
- `#EXT-X-MAP:URI` - Points to initialization segment
- `#EXT-X-TARGETDURATION` - Maximum segment duration (rounded up)
- `#EXT-X-MEDIA-SEQUENCE` - First segment sequence number
- `#EXTINF` - Actual duration of each segment

---

## 8. Latency Measurement System

### 8.1 Visual Timestamp Overlay

Each video frame contains a burned-in timestamp showing capture time:

```
┌─────────────────────────────────────────┐
│ 12:34:56.789                            │  ← Red timestamp overlay
│                                         │
│                                         │
│           [Video Content]               │
│                                         │
│                                         │
└─────────────────────────────────────────┘
```

This allows visual latency measurement by comparing displayed time with current time.

### 8.2 Segment Timestamp Tracking

The system monitors segment file creation using `inotify`:

```python
# When segment file is created/modified
segment_name = "segment00001.m4s"
creation_time = time.time_ns()  # Nanosecond precision

_SEGMENT_TIMESTAMPS[segment_name] = creation_time
```

### 8.3 Timestamp API

HTTP endpoint provides segment timestamps:

```
GET http://localhost:8080/timestamps.json
```

Response:
```json
{
  "segments": {
    "segment00001.m4s": 1734176000123456789,
    "segment00002.m4s": 1734176000623456789,
    "segment00003.m4s": 1734176001123456789
  },
  "timestamp": 1734176001500000000
}
```

### 8.4 Calculating Latency

```
End-to-End Latency = Current Time - Frame Capture Time

Where Frame Capture Time can be determined from:
1. Visual overlay in the video frame
2. Segment creation timestamp (approximate)

Example:
- Frame shows: 12:34:56.000
- Current time: 12:34:58.500
- Latency: 2.5 seconds
```

---

## Appendix A: File Structure

```
ll-hls/
├── streamer.py          # Main streaming application
├── cli.py               # Command-line argument parsing
├── requirements.txt     # Python dependencies
├── setup.sh             # Environment setup script
├── .venv/               # Python virtual environment
└── output/              # Generated HLS files
    ├── init00000.mp4    # Initialization segment
    ├── live.m3u8        # HLS playlist
    ├── segment00001.m4s # Media segment 1
    ├── segment00002.m4s # Media segment 2
    └── ...
```

## Appendix B: GStreamer Plugin Requirements

Custom plugins required (installed to `~/.local/share/gstreamer-1.0/plugins/`):

| Plugin | File | Purpose |
|--------|------|---------|
| hlssink3 | libgsthlssink3.so | HLS CMAF sink element |
| isobmff | libgstisobmff.so | CMAF/fMP4 muxer |

Built from: https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs

## Appendix C: Playback

### VLC

```bash
vlc http://192.168.0.84:8080/live.m3u8
```

### ffplay

```bash
ffplay -fflags nobuffer -flags low_delay http://192.168.0.84:8080/live.m3u8
```

### HLS.js (Browser)

```html
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<video id="video"></video>
<script>
  const video = document.getElementById('video');
  const hls = new Hls({
    lowLatencyMode: true,
    liveSyncDurationCount: 3
  });
  hls.loadSource('http://192.168.0.84:8080/live.m3u8');
  hls.attachMedia(video);
</script>
```
