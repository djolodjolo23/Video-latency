import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const STREAM_URL = 'http://192.168.0.84:8080/live.m3u8'; // most likely need to update on different networks
const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;

if (!executablePath) {
    throw new Error('Set PUPPETEER_EXECUTABLE_PATH to your Chrome/Chromium binary before running the client.');
}

async function main() {
    const browser = await puppeteer.launch({
        headless: true,
        executablePath
    });
    const page = await browser.newPage();

    page.on('pageerror', err => {
        const message = err instanceof Error ? err.message : String(err);
        console.error('[PAGEERROR]', message);
    });
    page.on('console', msg => {
        if (msg.type() === 'log' && msg.text().startsWith('LATENCY')) {
            const data = JSON.parse(msg.text().slice('LATENCY '.length));
            console.log(`[LATENCY] ${data.latencyMs.toFixed(1)} ms (produced at ${data.productionIso})`);
        } else {
            console.log('[BROWSER]', msg.text());
        }
    });

    const fileUrl = pathToFileURL(path.join(__dirname, 'browser.html'));
    fileUrl.searchParams.set('src', STREAM_URL);
    
    await page.goto(fileUrl.href, { waitUntil: 'load' });
    console.log(`Streaming from ${fileUrl.href} (source: ${STREAM_URL})...`);
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
