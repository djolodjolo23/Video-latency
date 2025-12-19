/**
 * Multi-Client QoE Benchmark for WebRTC
 * Usage: npx tsx webrtc/client/qoe-benchmark.ts --clients 10 --duration 60 --page http://localhost:8080/
 */

import fs from "node:fs";
import puppeteer, { Browser } from "puppeteer-core";
import { parseArgs } from "./qoe-config.js";
import { QoEClient } from "./qoe-client.js";
import { generateReports } from "../../commons/qoe/qoe-reporter.js";
import { ClientMetrics } from "../../commons/qoe/qoe-types.js";

class QoEBenchmark {
  private browsers: Browser[] = [];
  private clients: QoEClient[] = [];

  constructor(private config: ReturnType<typeof parseArgs>) {}

  async run(): Promise<void> {
    console.log("=".repeat(60));
    console.log("QoE Benchmark Configuration (WebRTC)");
    console.log("=".repeat(60));
    console.log(`  Clients:    ${this.config.numClients}`);
    console.log(`  Duration:   ${this.config.durationSec} seconds`);
    console.log(`  Page:       ${this.config.pageUrl}`);
    console.log(`  Output:     ${this.config.outputDir}`);
    console.log(`  Browsers:   ${this.config.numBrowsers}`);
    console.log(`  TTFF timeout: ${this.config.ttffTimeoutMs} ms`);
    console.log(`  Warmup:     ${this.config.warmup ? "yes" : "no"}`);
    console.log("=".repeat(60));
    console.log();

    fs.mkdirSync(this.config.outputDir, { recursive: true });

    const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
    if (!executablePath) {
      throw new Error("Set PUPPETEER_EXECUTABLE_PATH to your Chrome/Chromium binary");
    }

    console.log("Launching browser(s)...");
    for (let b = 0; b < this.config.numBrowsers; b++) {
      const browser = await puppeteer.launch({
        headless: this.config.headless,
        executablePath,
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-dev-shm-usage",
          "--disable-gpu",
          "--autoplay-policy=no-user-gesture-required",
        ],
      });
      this.browsers.push(browser);
      console.log(`  Browser ${b} ready`);
    }

    if (this.config.warmup) {
      console.log();
      console.log("Warmup: starting one page per browser (TTFF discarded)...");
      for (let i = 0; i < this.browsers.length; i++) {
        const result = await this.runWarmup(this.browsers[i], i);
        console.log(`  Warmup on browser ${i}: ${result}`);
        if (i < this.browsers.length - 1) {
          await this.sleep(200);
        }
      }
      console.log("Warmup complete. Launching measured clients...");
      console.log();
    }

    console.log(
      `Launching ${this.config.numClients} clients across ${this.config.numBrowsers} browser(s)...`
    );
    for (let i = 0; i < this.config.numClients; i++) {
      const browser = this.browsers[i % this.browsers.length];
      const client = new QoEClient(i, browser, this.config.pageUrl, this.config.ttffTimeoutMs);
      await client.start();
      this.clients.push(client);
      console.log(`  Client ${i} started`);

      if (i < this.config.numClients - 1 && this.config.staggerDelayMs > 0) {
        await this.sleep(this.config.staggerDelayMs);
      }
    }

    console.log();
    console.log(`All clients started. Running for ${this.config.durationSec} seconds...`);
    console.log();

    for (let second = 0; second < this.config.durationSec; second++) {
      await this.sleep(1000);

      for (const client of this.clients) {
        client.recordSecond(second + 1);
      }

      if ((second + 1) % 10 === 0) {
        console.log(`  Progress: ${second + 1}/${this.config.durationSec} seconds`);
      }
    }

    console.log();
    console.log("Benchmark complete. Collecting results...");

    const allMetrics: ClientMetrics[] = this.clients.map((c) => c.getMetrics());
    generateReports(allMetrics, this.config as any);

    for (const client of this.clients) {
      await client.close();
    }
    for (const browser of this.browsers) {
      await browser.close();
    }
  }

  private async runWarmup(browser: Browser, browserIndex: number): Promise<string> {
    const page = await browser.newPage();
    const url = new URL(this.config.pageUrl);
    url.searchParams.set("clientId", `warmup-${browserIndex}`);

    try {
      await page.goto(url.href, { waitUntil: "load" });
      await page.waitForFunction(
        () => {
          const v = document.querySelector("video");
          return !!v && v.readyState >= 2;
        },
        { timeout: this.config.warmupTimeoutMs }
      );
      return "ready";
    } catch (err) {
      const error = err instanceof Error ? err.message : String(err);
      return error.includes("Timeout") ? "timeout" : `error: ${error}`;
    } finally {
      await page.close();
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}

async function main() {
  const config = parseArgs();
  const benchmark = new QoEBenchmark(config);

  try {
    await benchmark.run();
  } catch (err) {
    console.error("Benchmark failed:", err);
    process.exit(1);
  }
}

main();
