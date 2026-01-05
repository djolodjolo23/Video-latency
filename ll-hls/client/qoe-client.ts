import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Browser, Page } from 'puppeteer-core';
import { ClientMetrics, QoEEvent, SecondMetrics } from '../../commons/qoe/qoe-types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export class QoEClient {
  private page: Page | null = null;
  private metrics: ClientMetrics;
  private currentSecondData: Partial<SecondMetrics> = {};
  private ttffTimer: NodeJS.Timeout | null = null;

  constructor(
    private clientId: number,
    private browser: Browser,
    private streamUrl: string,
    private ttffTimeoutMs: number
  ) {
    this.metrics = {
      clientId: String(clientId),
      ttffMs: null,
      latencySamples: [],
      stallCount: 0,
      totalStallDurationMs: 0,
      errors: [],
      secondBySecond: [],
    };
  }

  async start(): Promise<void> {
    this.page = await this.browser.newPage();

    this.ttffTimer = setTimeout(() => {
      if (this.metrics.ttffMs === null) {
        const msg = `TTFF timeout after ${this.ttffTimeoutMs} ms`;
        this.metrics.errors.push(msg);
        console.error(`[Client ${this.clientId}] Error: ${msg}`);
      }
    }, this.ttffTimeoutMs);
    
    this.page.on('console', msg => {
      const text = msg.text();
      if (text.startsWith('QOE ')) {
        try {
          const event: QoEEvent = JSON.parse(text.slice(4));
          this.handleQoEEvent(event);
        } catch {
          // ignore malformed JSON
        }
      }
    });

    this.page.on('pageerror', err => {
      this.metrics.errors.push(err instanceof Error ? err.message : String(err));
    });

    const fileUrl = pathToFileURL(path.join(__dirname, 'browser-qoe.html'));
    fileUrl.searchParams.set('src', this.streamUrl);
    fileUrl.searchParams.set('clientId', String(this.clientId));
    
    await this.page.goto(fileUrl.href, { waitUntil: 'load' });
  }

  recordSecond(second: number): SecondMetrics {
    const data: SecondMetrics = {
      second,
      latencyMs: this.currentSecondData.latencyMs ?? null,
      stallCount: this.currentSecondData.stallCount ?? 0,
      totalStallMs: this.currentSecondData.totalStallMs ?? 0,
      bufferAheadSec: this.currentSecondData.bufferAheadSec ?? 0,
      isStalling: this.currentSecondData.isStalling ?? false,
    };
    this.metrics.secondBySecond.push(data);
    return data;
  }

  getMetrics(): ClientMetrics {
    return this.metrics;
  }

  async close(): Promise<void> {
    if (this.ttffTimer) {
      clearTimeout(this.ttffTimer);
      this.ttffTimer = null;
    }
    if (this.page) {
      await this.page.close();
    }
  }

  private handleQoEEvent(event: QoEEvent): void {
    switch (event.type) {
      case 'debug':
        console.log(`[Client ${this.clientId}] DEBUG: ${event.message}`);
        break;
      case 'ttff':
        this.metrics.ttffMs = event.ttffMs;
        if (this.ttffTimer) {
          clearTimeout(this.ttffTimer);
          this.ttffTimer = null;
        }
        console.log(`[Client ${this.clientId}] TTFF: ${event.ttffMs.toFixed(0)} ms`);
        break;
      case 'latency':
        this.metrics.latencySamples.push(event.latencyMs);
        break;
      case 'stall_start':
        this.metrics.stallCount = event.stallCount;
        console.log(`[Client ${this.clientId}] Stall #${event.stallCount} started`);
        break;
      case 'stall_end':
        this.metrics.totalStallDurationMs += event.stallDurationMs;
        console.log(`[Client ${this.clientId}] Stall ended (${event.stallDurationMs.toFixed(0)} ms)`);
        break;
      case 'stats': {
        const second = Math.floor(event.elapsedMs / 1000);
        this.currentSecondData = {
          second,
          latencyMs: event.currentLatencyMs,
          stallCount: event.stallCount,
          totalStallMs: event.totalStallDurationMs,
          bufferAheadSec: event.bufferAheadSec,
          isStalling: event.isStalling,
        };
        break;
      }
      case 'error':
      case 'hls_error':
        this.metrics.errors.push(event.error || event.details || 'unknown');
        console.error(`[Client ${this.clientId}] Error: ${event.error || event.details}`);
        break;
    }
  }
}
