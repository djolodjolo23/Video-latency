Python-first WebRTC loopback to verify signaling and media flow. Use this to prove the pipeline works before focusing on latency and DVR.

## Quick start

```bash
cd webrtc
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py --port 8080
# open http://localhost:8080 in a recent Chrome/Firefox
```

What happens:
- Browser creates an SDP offer (via Socket.IO), sends it to the Python backend, and receives an answer.
- The backend pulls media from the host camera and returns it to the browser.
- You should see a remote video feed from the Linux host; that confirms WebRTC is working.

## Next steps for latency/DVR
- Latency probes: draw a timestamp overlay in JS and measure the delta between local and remote frames (e.g., `requestVideoFrameCallback`).
- DVR concept: subscribe to the incoming track on the server and write to disk (e.g., `MediaRecorder` in the browser or `aiortc`'s `MediaRecorder` on the backend).
- TURN for real networks: add a TURN server to `iceServers` if you test across NATs.
