# Low-latency HLS webcam streaming

This folder contains two LL-HLS streamer implementations:

1. **`streamer.py`** - GStreamer-based, short-segment HLS (CMAF)
2. **`streamer_biim.py`** - True LL-HLS with partial segments using biim library

## Quick Comparison

| Feature | `streamer.py` (GStreamer) | `streamer_biim.py` (biim) |
|---------|---------------------------|---------------------------|
| Partial segments (`EXT-X-PART`) | ❌ | ✅ |
| Blocking playlist reload | Limited | ✅ Full support |
| Preload hints | ❌ | ✅ |
| In-memory serving | ❌ (disk-based) | ✅ |
| Expected latency | ~1-2s | ~0.5-1s |
| Video source | V4L2/GStreamer | FFmpeg |
| Dependencies | GStreamer, PyGObject | FFmpeg, biim, aiohttp |

---

## Option 1: GStreamer-based Streamer (`streamer.py`)

This builds a GStreamer pipeline which captures frames from a webcam (or a 
synthetic test pattern), encodes them with latency-focused settings, produces 
fragmented MP4 CMAF chunks via `splitmuxsink`+`mp4mux`, writes a custom LL-HLS 
playlist (with `#EXT-X-PART` and blocking reload support), and serves everything 
through a lightweight HTTP server.

### Setup (one time)

Run the provided helper to install the necessary system packages, bootstrap a
Python virtual environment, and install Python dependencies:

```bash
cd ll-hls
bash setup.sh
```

- The script expects an apt-based system (Debian/Ubuntu). Adjust it as needed
  for other environments. It installs PyGObject/PyCairo via apt so there is no
  lengthy source build when creating a venv.
- A `.venv/` folder is placed inside `ll-hls/`. Keeping per-directory
  virtual environments prevents dependency clashes with the other experiments in
  this repo. The venv is created with `--system-site-packages` so the apt-provided
  GStreamer Python bindings remain accessible inside it.
- Reactivate the environment later with `source ll-hls/.venv/bin/activate`.

## Usage

1. Ensure your webcam is available as `/dev/video0` (adjust with `--device` if
   needed).
2. Activate the virtual environment created by `setup.sh`:
   ```bash
   source ll-hls/.venv/bin/activate
   ```
3. Run the streamer from the `ll-hls/` folder:
   ```bash
   cd ll-hls
   python3 streamer.py --device /dev/video0 --http-port 8080
   ```
4. From another machine on the network, point an LL-HLS capable player to
   `http://<streamer-ip>:8080/live.m3u8` (Safari, ffplay, VLC ≥ 4, hls.js, etc).

The helper exposes several CLI switches so you can trade latency vs quality:

| Flag | Description |
| --- | --- |
| `--resolution 1280x720` | Output frame size (WIDTHxHEIGHT). |
| `--framerate 30` | Frames per second. |
| `--bitrate 2500` | Encoder bitrate in kbit/s. |
| `--segment-duration 1.0` | Segment length (keep ≤1s for LL-HLS). |
| `--part-duration 0.333` | Target CMAF part duration (the helper emits one part per segment). |
| `--playlist-length 6` | Visible segments in the playlist. |
| `--max-files 20` | How many segments to retain on disk. |
| `--test-src` | Replace the webcam with a moving colorbars pattern. |
| `--public-url http://ip:8080` | URL announced inside `#EXT-X-MAP`/segments (set when binding `0.0.0.0`). |

Run `python3 streamer.py --help` to see the full list. The generated playlist
supports **blocking reloads**: LL-HLS clients can append `?wait=500&version=<n>`
when polling `live.m3u8` to keep the HTTP connection open until newer parts are
available (or 500 ms elapse).

## Timestamp Injection & Latency Measurement

The streamer provides timestamp tracking for latency measurements via a JSON sidecar approach:

- **Segment monitoring**: Uses `inotify` (Linux filesystem events) to detect new CMAF `.m4s` files immediately
- **Timestamp capture**: Records Unix nanosecond timestamp when each segment file is fully written (`CLOSE_WRITE` event)
- **JSON endpoint**: Serves timestamp mapping at `/timestamps.json` (updated in real-time)
- **Client correlation**: Clients poll the endpoint every 500ms and match segment numbers to timestamps
- **Fallback**: Falls back to polling-based monitoring if `inotify` is unavailable

This out-of-band approach works around HLS/CMAF limitations where the writer doesn't expose ID3 tagging hooks.

**Implementation details:**
- Event-driven monitoring via `inotify` for zero polling overhead
- Background daemon thread tracks segments without blocking the pipeline
- Shared dictionary (`_SEGMENT_TIMESTAMPS`) for thread-safe timestamp access
- Automatic cleanup (keeps last 20 segments in memory)

**Requirements for accurate measurements:**
- Both server and client must have NTP time synchronization enabled
- Install chrony on server: `sudo apt install chrony && sudo systemctl enable --now chrony`
- Verify sync: `chronyc tracking` (offset should be < 50ms)
- On macOS clients, ensure "Set date and time automatically" is enabled in System Settings

**Python dependencies:**
- `inotify-simple` for efficient filesystem monitoring (installed via `setup.sh`)

## Anatomy

The script wires up the following pipeline:

```
v4l2src → videoconvert → videoscale → videorate → x264enc (zerolatency)
       → h264parse → splitmuxsink+mp4mux (writes CMAF fragments) → HTTP file server
```

---

## Option 2: True LL-HLS Streamer (`streamer_biim.py`)

This uses FFmpeg for video capture/encoding and the [biim](https://github.com/monyone/biim) 
library for true Apple LL-HLS packaging with partial segments.

### Setup

```bash
cd ll-hls
source .venv/bin/activate  # Or create a new venv
pip install biim aiohttp
```

Ensure FFmpeg is installed:
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### Usage

```bash
# With test source
python3 streamer_biim.py --test-src --http-port 8080

# With webcam (Linux)
python3 streamer_biim.py --device /dev/video0 --http-port 8080
```

### CLI Options

| Flag | Description |
| --- | --- |
| `--device /dev/video0` | V4L2 device to capture from |
| `--test-src` | Use FFmpeg test pattern instead of webcam |
| `--resolution 1280x720` | Output frame size |
| `--framerate 30` | Frames per second |
| `--bitrate 2500` | Encoder bitrate in kbit/s |
| `--target-duration 1` | Segment duration in seconds |
| `--part-duration 0.1` | LL-HLS part duration (default: 100ms) |
| `--window-size 10` | Segments in live window |
| `--http-port 8080` | HTTP server port |

### Endpoints

- `http://localhost:8080/playlist.m3u8` - LL-HLS playlist
- `http://localhost:8080/live.m3u8` - Alias for compatibility
- `http://localhost:8080/timestamps.json` - Timestamp data for latency measurement

### How It Works

```
FFmpeg (capture + encode) → MPEG-TS pipe → biim (demux + LL-HLS package) → aiohttp (serve)
```

biim processes the MPEG-TS stream in real-time, creating:
- Partial segments (parts) every ~100ms
- Full segments every ~1s
- Dynamic playlist with `EXT-X-PART`, `EXT-X-PRELOAD-HINT`
- Blocking playlist reload support

All content is served from memory with no disk I/O.

---

## Client-Side Testing

Both streamers are compatible with the same client tooling:

```bash
# Run the TypeScript client
cd ll-hls/client
npx tsx client.ts http://localhost:8080/live.m3u8
```

Or open `client/browser.html` directly in a browser with the `?src=` parameter.
