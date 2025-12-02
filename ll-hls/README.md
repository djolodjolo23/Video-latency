# Low-latency HLS webcam streaming

This folder contains the LL-HLS helper (`streamer.py`). It builds a GStreamer
pipeline which captures frames from a webcam (or a synthetic test pattern),
encodes them with latency-focused settings, produces LL-HLS compatible CMAF
segments via `hlssink2`, and serves them through a lightweight HTTP server so
that another device can tune in using a standard HLS player. The client-side
measurement tooling is shared across transports and lives in the common helpers
directory (outside this folder).

## Setup (one time)

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
| `--playlist-length 6` | Visible segments in the playlist. |
| `--max-files 20` | How many segments to retain on disk. |
| `--test-src` | Replace the webcam with a moving colorbars pattern. |
| `--public-url http://ip:8080` | URL announced inside `#EXT-X-MAP`/segments (set when binding `0.0.0.0`). |

Run `python3 streamer.py --help` to see the full list.

## Timestamp Injection & Latency Measurement

The streamer provides timestamp tracking for latency measurements via a JSON sidecar approach:

- **Segment monitoring**: Uses `inotify` (Linux filesystem events) to detect new segment files immediately
- **Timestamp capture**: Records Unix nanosecond timestamp when each segment file is fully written (`CLOSE_WRITE` event)
- **JSON endpoint**: Serves timestamp mapping at `/timestamps.json` (updated in real-time)
- **Client correlation**: Clients poll the endpoint every 500ms and match segment numbers to timestamps
- **Fallback**: Falls back to polling-based monitoring if `inotify` is unavailable

This out-of-band approach works around HLS/MPEG-TS limitations where GStreamer's `hlssink2` doesn't support embedding ID3 tags or custom metadata in segments.

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
       → h264parse → hlssink2 (writes CMAF/HLS parts) → HTTP file server
```
