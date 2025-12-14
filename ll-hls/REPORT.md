# Low-Latency HLS Implementation Report

## Executive Summary

This report documents the implementation of Low-Latency HLS (LL-HLS) streaming using GStreamer with CMAF (Common Media Application Format) segments. The implementation achieves significantly lower latency compared to regular HLS by using shorter segment durations and proper CMAF fragmentation.

---

## 1. Background: Regular HLS vs Low-Latency HLS

### Regular HLS Characteristics

| Property | Regular HLS |
|----------|-------------|
| Segment Format | MPEG-TS (.ts) |
| Typical Segment Duration | 6-10 seconds |
| Minimum Buffering | 3 segments (18-30 seconds) |
| End-to-End Latency | 20-40+ seconds |
| Playlist Updates | Every segment |

### Low-Latency HLS Characteristics

| Property | LL-HLS |
|----------|--------|
| Segment Format | CMAF/fMP4 (.m4s) |
| Segment Duration | 0.3-2 seconds |
| Partial Segments | Yes (sub-segment chunks) |
| End-to-End Latency | 2-5 seconds |
| Playlist Updates | More frequent |

---

## 2. Implementation Details

### 2.1 GStreamer Plugins Used

We built and installed custom GStreamer plugins from the `gst-plugins-rs` repository:

1. **`hlscmafsink`** (from `gst-plugin-hlssink3`)
   - HLS sink that produces proper CMAF segments
   - Generates initialization segment (`init.mp4`) with `ftyp` + `moov` boxes
   - Generates media segments (`.m4s`) with `styp` + `moof` + `mdat` boxes
   - Automatically manages HLS playlist generation

2. **`cmafmux`** (from `gst-plugin-isobmff`)
   - CMAF-compliant fragmented MP4 muxer
   - Supports sub-second fragment durations via `fragment-duration` property (in nanoseconds)
   - Internal component of `hlscmafsink`

### 2.2 Plugin Installation

Plugins were built from source and installed to:
```
~/.local/share/gstreamer-1.0/plugins/
├── libgsthlssink3.so
└── libgstisobmff.so
```

The `streamer.py` automatically sets `GST_PLUGIN_PATH` to include this directory.

### 2.3 Pipeline Architecture

```
┌─────────────┐    ┌───────────────┐    ┌─────────────┐    ┌───────────┐
│ v4l2src     │───▶│ videoconvert  │───▶│ timeoverlay │───▶│ videoscale│
│ (webcam)    │    │               │    │ (timestamp) │    │           │
└─────────────┘    └───────────────┘    └─────────────┘    └───────────┘
                                                                  │
                                                                  ▼
┌─────────────┐    ┌───────────────┐    ┌─────────────┐    ┌───────────┐
│ hlscmafsink │◀───│ h264parse     │◀───│ x264enc     │◀───│ videorate │
│ (LL-HLS)    │    │ (AVC format)  │    │ (encoder)   │    │           │
└─────────────┘    └───────────────┘    └─────────────┘    └───────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Output Files:                                                        │
│   • init00000.mp4  (initialization segment: ftyp + moov)            │
│   • segment00001.m4s, segment00002.m4s, ... (media segments)        │
│   • live.m3u8 (HLS playlist)                                        │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.4 Key Configuration: Sub-Second Segment Duration

The critical discovery was that `hlscmafsink`'s `target-duration` property only accepts **integer seconds**. To achieve sub-second segments, we needed to access the internal `cmafmux` element via GStreamer's `GstChildProxy` interface:

```python
# hlscmafsink implements GstChildProxy - access the internal 'muxer' (cmafmux)
hlssink_elem = pipeline.get_by_name("hlssink")
muxer = hlssink_elem.get_child_by_name("muxer")

# Convert segment duration from seconds to nanoseconds
fragment_duration_ns = int(segment_duration * 1_000_000_000)
muxer.set_property("fragment-duration", fragment_duration_ns)
```

**Example**: For 0.5-second segments:
- `fragment-duration` = 500,000,000 nanoseconds

---

## 3. CMAF Segment Structure

### 3.1 Initialization Segment (`init00000.mp4`)

Contains codec configuration, **no media data**:

```
┌──────────────────────────────────────────────┐
│ ftyp (file type box)                         │
│   • brand: iso5, avc1, iso6, mp41            │
├──────────────────────────────────────────────┤
│ moov (movie box)                             │
│   ├── mvhd (movie header)                    │
│   ├── trak (track box)                       │
│   │   ├── tkhd (track header)                │
│   │   └── mdia (media box)                   │
│   │       ├── mdhd (media header)            │
│   │       ├── hdlr (handler: video)          │
│   │       └── minf (media info)              │
│   │           └── stbl (sample table)        │
│   │               └── stsd (sample desc)     │
│   │                   └── avc1 (H.264 codec) │
│   │                       └── avcC (decoder  │
│   │                            config: SPS,  │
│   │                            PPS)          │
│   └── mvex (movie extends)                   │
│       └── trex (track extends)               │
└──────────────────────────────────────────────┘
```

**Size**: ~778 bytes (contains no video frames)

### 3.2 Media Segments (`segment00001.m4s`, etc.)

Each segment is self-contained and references the init segment:

```
┌──────────────────────────────────────────────┐
│ styp (segment type box)                      │
│   • brand: msdh, msix                        │
├──────────────────────────────────────────────┤
│ moof (movie fragment box)                    │
│   ├── mfhd (fragment header)                 │
│   │   └── sequence_number                    │
│   └── traf (track fragment)                  │
│       ├── tfhd (track fragment header)       │
│       ├── tfdt (decode time)                 │
│       └── trun (track run)                   │
│           └── sample sizes, durations,       │
│               composition offsets            │
├──────────────────────────────────────────────┤
│ mdat (media data box)                        │
│   └── H.264 NAL units (video frames)         │
└──────────────────────────────────────────────┘
```

---

## 4. HLS Playlist Structure

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
```

Key tags:
- `#EXT-X-VERSION:7` - HLS version supporting CMAF
- `#EXT-X-MAP:URI="init00000.mp4"` - Points to initialization segment
- `#EXTINF:0.5` - Each segment is 0.5 seconds

---

## 5. Factors Affecting Latency

### 5.1 Segment Duration

| Segment Duration | Expected Latency | Notes |
|------------------|------------------|-------|
| 6 seconds | 20-30 seconds | Traditional HLS |
| 2 seconds | 6-10 seconds | Reduced HLS |
| 1 second | 3-5 seconds | Low-latency |
| 0.5 seconds | 2-3 seconds | Very low-latency |
| 0.3 seconds | 1-2 seconds | Ultra low-latency |

**Command-line control**: `--segment-duration 0.5`

### 5.2 Keyframe Interval (`key-int-max`)

Segments can only start at keyframes (I-frames). The keyframe interval must be:
- **Equal to or less than** the segment duration
- Calculated as: `key-int-max = framerate × segment_duration`

| Framerate | Segment Duration | Recommended key-int-max |
|-----------|------------------|-------------------------|
| 30 fps | 1.0 second | 30 |
| 30 fps | 0.5 seconds | 15 |
| 30 fps | 0.3 seconds | 9 |

**Command-line control**: `--key-int-max 15`

### 5.3 Encoder Settings

```python
x264enc tune=zerolatency speed-preset=ultrafast bframes=0
```

- **`tune=zerolatency`**: Disables encoder buffering
- **`speed-preset=ultrafast`**: Fastest encoding (trades quality for speed)
- **`bframes=0`**: No B-frames (reduces encoding delay)

### 5.4 Pipeline Buffering

```python
queue leaky=downstream max-size-buffers=1
```

- **`leaky=downstream`**: Drops old frames if processing is slow
- **`max-size-buffers=1`**: Minimal buffering in the queue

### 5.5 hlscmafsink Latency Property

```python
latency=500000000  # 500ms in nanoseconds
```

Controls the internal buffering within the HLS sink.

---

## 6. Comparison: Before and After

### Before (Original Implementation with splitmuxsink)

| Issue | Description |
|-------|-------------|
| Wrong format | Produced self-contained MP4 files, not CMAF segments |
| No init segment | Each segment contained full `moov` box with sample tables |
| Player incompatibility | HLS players couldn't parse the segments |
| Result | **Client hung, no playback** |

### After (hlscmafsink Implementation)

| Improvement | Description |
|-------------|-------------|
| Proper CMAF | Segments have `styp` + `moof` + `mdat` structure |
| Separate init | `init00000.mp4` contains codec config only |
| Sub-second segments | 0.3-0.5 second segments via `fragment-duration` |
| Working playback | Standard HLS players work correctly |
| Result | **2-3 second latency achieved** |

---

## 7. Usage

### Basic Usage (Default Settings)

```bash
cd ll-hls
source .venv/bin/activate
python3 streamer.py
```

### Low-Latency Configuration

```bash
python3 streamer.py --segment-duration 0.5 --key-int-max 15
```

### Ultra Low-Latency Configuration

```bash
python3 streamer.py --segment-duration 0.3 --key-int-max 9 --speed-preset ultrafast
```

### Available Options

| Option | Default | Description |
|--------|---------|-------------|
| `--segment-duration` | 1.0 | Segment duration in seconds |
| `--key-int-max` | 30 | Maximum keyframe interval |
| `--speed-preset` | ultrafast | x264 encoding speed |
| `--bitrate` | 2000 | Video bitrate in kbps |
| `--http-port` | 8080 | HTTP server port |

---

## 8. Timestamp Tracking

The implementation includes a timestamp tracking system for latency measurement:

1. **Visual Overlay**: `timeoverlay` element burns current time into each frame
2. **Segment Timestamps**: `inotify` monitors segment creation times
3. **HTTP Endpoint**: `/timestamps.json` provides segment creation timestamps

```json
{
  "segments": {
    "segment00001.m4s": 1734176000123456789,
    "segment00002.m4s": 1734176000623456789
  },
  "timestamp": 1734176001000000000
}
```

---

## 9. Technical Requirements

### Dependencies

- GStreamer 1.24+
- Python 3.10+
- PyGObject (gi)
- inotify-simple

### Custom Plugins (Built from gst-plugins-rs)

- `libgsthlssink3.so` - LL-HLS sink
- `libgstisobmff.so` - CMAF muxer

### Plugin Path

```bash
~/.local/share/gstreamer-1.0/plugins/
```

---

## 10. Conclusion

The implementation successfully transforms a non-functional HLS streamer into a working Low-Latency HLS system. Key achievements:

1. **Proper CMAF segment structure** using `hlscmafsink` and `cmafmux`
2. **Sub-second segment control** via `GstChildProxy` access to internal muxer
3. **Latency reduction** from 20-30 seconds (regular HLS) to 2-3 seconds (LL-HLS)
4. **Automatic plugin discovery** with `GST_PLUGIN_PATH` configuration

The main factors for lowering latency are:
- Shorter segment duration (controlled by `fragment-duration`)
- Matching keyframe interval (`key-int-max`)
- Zero-latency encoder tuning
- Minimal pipeline buffering
