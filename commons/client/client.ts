import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const STREAM_URL = 'http://localhost:8080/live.m3u8';
const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;

if (!executablePath) {
    throw new Error('Set PUPPETEER_EXECUTABLE_PATH to your Chrome/Chromium binary before running the client.');
}

const html = `
<!DOCTYPE html>
<html>
  <body>
    <video id="video" autoplay muted playsinline></video>
    <script type="module">
      import Hls from 'https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js';
      const video = document.getElementById('video');
      const hls = new Hls({ enableWorker: true });
      // Expose for debugging in devtools.
      Object.assign(window, { hls, Hls, video });
      window.reportLatency = latency => console.log('LATENCY', JSON.stringify(latency));

      hls.on(Hls.Events.FRAG_PARSING_METADATA, (_event, data) => {
        for (const sample of data.samples) {
          const view = new DataView(sample.data.buffer, sample.data.byteOffset, sample.data.byteLength);
          const high = view.getBigUint64(0);
          const low = view.getBigUint64(8);
          const productionNs = (high << 64n) | low;
          const productionMs = Number(productionNs / 1_000_000n);
          const clientMs = performance.now() + performance.timeOrigin;
          window.reportLatency({
            productionIso: new Date(productionMs).toISOString(),
            latencyMs: clientMs - productionMs
          });
        }
      });

      hls.loadSource(${JSON.stringify(STREAM_URL)});
      hls.attachMedia(video);
    </script>
  </body>
</html>
`;

async function main() {
    const browser = await puppeteer.launch({
        headless: true,
        executablePath
    });
    const page = await browser.newPage();

    page.on('console', msg => {
        if (msg.type() === 'log' && msg.text().startsWith('LATENCY')) {
            const data = JSON.parse(msg.text().slice('LATENCY '.length));
            console.log(`[LATENCY] ${data.latencyMs.toFixed(1)} ms (produced at ${data.productionIso})`);
        } else {
            console.log('[BROWSER]', msg.text());
        }
    });

    await page.setContent(html, { waitUntil: 'networkidle0' });
    console.log(`Streaming from ${STREAM_URL}...`);
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
