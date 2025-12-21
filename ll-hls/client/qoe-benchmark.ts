/**
 * Multi-Client QoE Benchmark for LL-HLS
 * Usage: npx tsx ll-hls/client/qoe-benchmark.ts --clients 10 --duration 60 --stream http://localhost:8080/live.m3u8
 */

import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { execSync } from 'node:child_process';
import puppeteer, { Browser } from 'puppeteer-core';
import { Config, parseArgs } from './qoe-config.js';
import { QoEClient } from './qoe-client.js';
import { generateReports } from '../../commons/qoe/qoe-reporter.js';
import { ClientMetrics } from '../../commons/qoe/qoe-types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

type CpuSnapshot = { idle: number; total: number };
type MemSnapshot = { usedBytes: number; totalBytes: number; usedPct: number };

class SystemMonitor {
  private timer: NodeJS.Timeout | null = null;
  private prevCpu: CpuSnapshot | null = null;
  private samples: Array<{ cpuPct: number; memPct: number; usedBytes: number; totalBytes: number }> = [];
  private startTime: number | null = null;

  constructor(
    private outputPath: string,
    private intervalMs: number,
    private protocol: string,
    private numClients: number
  ) {}

  start(): void {
    this.startTime = Date.now();
    this.prevCpu = this.readCpu();
    this.writeHeaderIfNeeded();
    this.timer = setInterval(() => this.sample(), this.intervalMs);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.writeSummary();
  }

  private sample(): void {
    const current = this.readCpu();
    const cpuPct = this.prevCpu ? this.calcCpuPct(this.prevCpu, current) : 0;
    this.prevCpu = current;

    const mem = this.readMem();
    this.samples.push({
      cpuPct,
      memPct: mem.usedPct,
      usedBytes: mem.usedBytes,
      totalBytes: mem.totalBytes,
    });
  }

  private readCpu(): CpuSnapshot {
    let idle = 0;
    let total = 0;
    for (const core of os.cpus()) {
      idle += core.times.idle;
      total += Object.values(core.times).reduce((sum, value) => sum + value, 0);
    }
    return { idle, total };
  }

  private calcCpuPct(prev: CpuSnapshot, next: CpuSnapshot): number {
    const idleDelta = next.idle - prev.idle;
    const totalDelta = next.total - prev.total;
    if (totalDelta <= 0) return 0;
    return (1 - idleDelta / totalDelta) * 100;
  }

  private readMem(): MemSnapshot {
    const total = os.totalmem();
    if (os.platform() !== 'darwin') {
      const used = total - os.freemem();
      return { usedBytes: used, totalBytes: total, usedPct: (used / total) * 100 };
    }

    try {
      const output = execSync('vm_stat', { encoding: 'utf8' });
      const pageSizeMatch = output.match(/page size of (\d+) bytes/i);
      const pageSize = pageSizeMatch ? parseInt(pageSizeMatch[1], 10) : 4096;
      const readPages = (label: string): number => {
        const match = output.match(new RegExp(`${label}:\\s+(\\d+)`, 'i'));
        return match ? parseInt(match[1], 10) : 0;
      };
      const free = readPages('Pages free');
      const inactive = readPages('Pages inactive');
      const speculative = readPages('Pages speculative');
      const available = (free + inactive + speculative) * pageSize;
      const used = Math.max(total - available, 0);
      return { usedBytes: used, totalBytes: total, usedPct: (used / total) * 100 };
    } catch {
      const used = total - os.freemem();
      return { usedBytes: used, totalBytes: total, usedPct: (used / total) * 100 };
    }
  }

  private writeHeaderIfNeeded(): void {
    if (fs.existsSync(this.outputPath)) return;
    const header = 'timestamp,protocol,num_clients,avg_cpu_pct,avg_mem_pct,avg_mem_used_gb,mem_total_gb,samples,duration_sec\n';
    fs.writeFileSync(this.outputPath, header);
  }

  private writeSummary(): void {
    if (!this.samples.length || this.startTime === null) return;
    const avg = (values: number[]) => values.reduce((sum, v) => sum + v, 0) / values.length;
    const avgCpu = avg(this.samples.map(s => s.cpuPct));
    const avgMem = avg(this.samples.map(s => s.memPct));
    const avgUsed = avg(this.samples.map(s => s.usedBytes));
    const totalBytes = this.samples[this.samples.length - 1].totalBytes;
    const durationSec = (Date.now() - this.startTime) / 1000;

    const row = [
      new Date().toISOString(),
      this.protocol,
      this.numClients,
      avgCpu.toFixed(1),
      avgMem.toFixed(1),
      (avgUsed / (1024 ** 3)).toFixed(2),
      (totalBytes / (1024 ** 3)).toFixed(2),
      this.samples.length,
      durationSec.toFixed(1),
    ].join(',') + '\n';

    fs.appendFileSync(this.outputPath, row);
  }
}

// runner
class QoEBenchmark {
  private browsers: Browser[] = [];
  private clients: QoEClient[] = [];
  
  constructor(private config: Config) {}

  async run(): Promise<void> {
    console.log('='.repeat(60));
    console.log('QoE Benchmark Configuration');
    console.log('='.repeat(60));
    console.log(`  Clients:    ${this.config.numClients}`);
    console.log(`  Duration:   ${this.config.durationSec} seconds`);
    console.log(`  Stream:     ${this.config.streamUrl}`);
    console.log(`  Output:     ${this.config.outputDir}`);
    console.log(`  Browsers:   ${this.config.numBrowsers}`);
    console.log(`  TTFF timeout: ${this.config.ttffTimeoutMs} ms`);
    console.log(`  Warmup:     ${this.config.warmup ? 'yes' : 'no'}`);
    console.log('='.repeat(60));
    console.log();

    // Ensure output directory exists
    fs.mkdirSync(this.config.outputDir, { recursive: true });

    const monitor = this.config.systemMetrics
      ? new SystemMonitor(
          this.config.systemMetricsOutput,
          this.config.systemMetricsIntervalSec * 1000,
          'll-hls',
          this.config.numClients
        )
      : null;
    let shuttingDown = false;
    const cleanup = async (exitCode?: number) => {
      if (shuttingDown) return;
      shuttingDown = true;
      if (monitor) {
        monitor.stop();
      }
      for (const client of this.clients) {
        await client.close();
      }
      for (const browser of this.browsers) {
        await this.closeBrowser(browser);
      }
      if (exitCode !== undefined) {
        process.exit(exitCode);
      }
    };
    const handleSignal = (signal: NodeJS.Signals) => {
      console.log(`\nReceived ${signal}. Shutting down...`);
      void cleanup(130);
    };
    process.once('SIGINT', handleSignal);
    process.once('SIGTERM', handleSignal);

    try {
      if (monitor) {
        monitor.start();
      }

      // Launch browser
      const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
      if (!executablePath) {
        throw new Error('Set PUPPETEER_EXECUTABLE_PATH to your Chrome/Chromium binary');
      }

    console.log('Launching browser(s)...');
    for (let b = 0; b < this.config.numBrowsers; b++) {
      const browser = await this.launchBrowser(executablePath);
      this.browsers.push(browser);
      console.log(`  Browser ${b} ready`);
    }

    // must warmup, cold start causes spike on ttff
    if (this.config.warmup) {
      console.log();
      for (let i = 0; i < this.browsers.length; i++) {
        const result = await this.runWarmup(this.browsers[i], i);
        console.log(`  Warmup on browser ${i}: ${result}`);
        if (i < this.browsers.length - 1) {
          await this.sleep(200); // brief pause between warmups
        }
      }
      console.log('Warmup complete. Launching measured clients...');
      console.log();
    }

    // Launch clients with staggered delay
    console.log(`Launching ${this.config.numClients} clients across ${this.config.numBrowsers} browser(s)...`);
    for (let i = 0; i < this.config.numClients; i++) {
      let browserIndex = i % this.browsers.length;
      let browser = this.browsers[browserIndex];
      let client: QoEClient | null = null;
      let started = false;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          client = new QoEClient(i, browser, this.config.streamUrl, this.config.ttffTimeoutMs);
          await client.start();
          started = true;
          break;
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          console.warn(`  Client ${i} start failed (attempt ${attempt}): ${msg}`);
          try {
            await this.closeBrowser(browser);
          } catch {
            // ignore close failures
          }
          browser = await this.launchBrowser(executablePath);
          this.browsers[browserIndex] = browser;
          await this.sleep(300);
        }
      }
      if (!started || !client) {
        throw new Error(`Failed to start client ${i}`);
      }
      this.clients.push(client);
      console.log(`  Client ${i} started`);

      if (i < this.config.numClients - 1) {
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
      console.log('Benchmark complete. Collecting results...');

      const allMetrics: ClientMetrics[] = this.clients.map(c => c.getMetrics());
      generateReports(allMetrics, this.config);
      await cleanup();
    } finally {
      process.off('SIGINT', handleSignal);
      process.off('SIGTERM', handleSignal);
      await cleanup();
    }
  }

  private async runWarmup(browser: Browser, browserIndex: number): Promise<string> {
    const page = await browser.newPage();
    const fileUrl = pathToFileURL(path.join(__dirname, 'browser-qoe.html'));
    fileUrl.searchParams.set('src', this.config.streamUrl);
    fileUrl.searchParams.set('clientId', `warmup-${browserIndex}`);

    try {
      await page.goto(fileUrl.href, { waitUntil: 'load' });
      await page.waitForFunction(
        () => {
          const v = document.querySelector('video');
          return !!v && v.readyState >= 2; // have current data
        },
        { timeout: this.config.warmupTimeoutMs }
      );
      return 'ready';
    } catch (err: any) {
      return err?.name === 'TimeoutError' ? 'timeout' : `error: ${err.message || err}`;
    } finally {
      await page.close();
    }
  }


  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  private async closeBrowser(browser: Browser): Promise<void> {
    const proc = browser.process();
    const timeoutMs = 5000;
    try {
      await Promise.race([
        browser.close(),
        new Promise((_, reject) => setTimeout(() => reject(new Error('close timeout')), timeoutMs)),
      ]);
    } catch {
      if (proc && !proc.killed) {
        try {
          proc.kill('SIGKILL');
        } catch {
          // ignore kill failures
        }
      }
    }
  }

  private async launchBrowser(executablePath: string): Promise<Browser> {
    return puppeteer.launch({
      headless: this.config.headless,
      executablePath,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-web-security',           // Allow file:// to fetch http://
        '--disable-features=IsolateOrigins', // Disable origin isolation
        '--disable-site-isolation-trials',   // Disable site isolation
        '--allow-file-access-from-files',    // Allow file:// origins to access other files
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-features=CalculateNativeWinOcclusion',
      ],
    });
  }
}

// Main
async function main() {
  const config = parseArgs();
  const benchmark = new QoEBenchmark(config);
  
  try {
    await benchmark.run();
  } catch (err) {
    console.error('Benchmark failed:', err);
    process.exit(1);
  }
}

main();
