import { Browser, Page } from "puppeteer-core";
import { ClientMetrics, QoEEvent, SecondMetrics } from "../../commons/qoe/qoe-types.js";

export class QoEClient {
  private page: Page | null = null;
  private metrics: ClientMetrics;
  private currentSecondData: Partial<SecondMetrics> = {};
  private ttffTimer: NodeJS.Timeout | null = null;
  private lastLatencySpikeMs: number | null = null;
  private static readonly LATENCY_SPIKE_THRESHOLD_MS = 200;

  constructor(
    private clientId: number,
    private browser: Browser,
    private pageUrl: string,
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

    this.page.on("console", (msg) => {
      const text = msg.text();
      if (text.startsWith("QOE ")) {
        try {
          const event: QoEEvent = JSON.parse(text.slice(4));
          this.handleQoEEvent(event);
        } catch {
          // ignore malformed JSON
        }
        return;
      }
      if (text.startsWith("[QOE DEBUG]")) {
        console.log(`[Client ${this.clientId}] ${text}`);
      }
    });

    this.page.on("pageerror", (err) => {
      this.metrics.errors.push(err instanceof Error ? err.message : String(err));
    });

    const url = new URL(this.pageUrl);
    url.searchParams.set("clientId", String(this.clientId));
    await this.page.goto(url.href, { waitUntil: "load" });
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
      try {
        await this.page.evaluate(() => {
          const shutdown = (window as any).__qoeShutdown;
          if (typeof shutdown === "function") {
            shutdown();
          }
        });
      } catch {
        // ignore failures during shutdown
      }
      await this.page.close({ runBeforeUnload: true });
    }
  }

  private handleQoEEvent(event: QoEEvent): void {
    switch (event.type) {
      case "debug":
        console.log(`[Client ${this.clientId}] DEBUG: ${event.message}`);
        break;
      case "ttff":
        this.metrics.ttffMs = event.ttffMs;
        if (this.ttffTimer) {
          clearTimeout(this.ttffTimer);
          this.ttffTimer = null;
        }
        console.log(`[Client ${this.clientId}] TTFF: ${event.ttffMs.toFixed(0)} ms`);
        break;
      case "latency":
        this.metrics.latencySamples.push(event.latencyMs);
        break;
      case "stall_start":
        this.metrics.stallCount = event.stallCount;
        console.log(`[Client ${this.clientId}] Stall #${event.stallCount} started`);
        break;
      case "stall_end":
        this.metrics.totalStallDurationMs += event.stallDurationMs;
        console.log(`[Client ${this.clientId}] Stall ended (${event.stallDurationMs.toFixed(0)} ms)`);
        break;
      case "stats": {
        const second = Math.floor(event.elapsedMs / 1000);
        this.currentSecondData = {
          second,
          latencyMs: event.currentLatencyMs,
          stallCount: event.stallCount,
          totalStallMs: event.totalStallDurationMs,
          bufferAheadSec: 0,
          isStalling: event.isStalling,
        };
        const currentLatencyMs = event.currentLatencyMs;
        if (
          typeof currentLatencyMs === "number" &&
          currentLatencyMs > QoEClient.LATENCY_SPIKE_THRESHOLD_MS &&
          currentLatencyMs !== this.lastLatencySpikeMs
        ) {
          this.lastLatencySpikeMs = currentLatencyMs;
          console.log(
            `[Client ${this.clientId}] [QOE DEBUG] stats latency spike ` +
              JSON.stringify({
                latencyMs: currentLatencyMs,
                latencySource: event.latencySource,
                clockOffsetMs: event.clockOffsetMs,
                rttMs: event.rttMs,
                second,
              })
          );
        }
        break;
      }
      case "error":
        this.metrics.errors.push(event.error || "unknown");
        console.error(`[Client ${this.clientId}] Error: ${event.error}`);
        break;
    }
  }
}
