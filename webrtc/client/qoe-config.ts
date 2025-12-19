import path from "node:path";
import { fileURLToPath } from "node:url";

export interface Config {
  numClients: number;
  durationSec: number;
  pageUrl: string;
  outputDir: string;
  headless: boolean;
  staggerDelayMs: number;
  numBrowsers: number;
  ttffTimeoutMs: number;
  warmup: boolean;
  warmupTimeoutMs: number;
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export function parseArgs(): Config {
  const args = process.argv.slice(2);
  const config: Config = {
    numClients: 1,
    durationSec: 60,
    pageUrl: "http://localhost:8080/",
    outputDir: path.join(__dirname, "qoe-results"),
    headless: true,
    staggerDelayMs: 0,
    numBrowsers: 1,
    ttffTimeoutMs: 10000,
    warmup: false,
    warmupTimeoutMs: 7000,
  };

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case "--clients":
      case "-c":
        config.numClients = parseInt(args[++i], 10);
        break;
      case "--duration":
      case "-d":
        config.durationSec = parseInt(args[++i], 10);
        break;
      case "--page":
      case "-p":
        config.pageUrl = args[++i];
        break;
      case "--output":
      case "-o":
        config.outputDir = args[++i];
        break;
      case "--headed":
        config.headless = false;
        break;
      case "--stagger":
        config.staggerDelayMs = parseInt(args[++i], 10);
        break;
      case "--browsers":
        config.numBrowsers = parseInt(args[++i], 10);
        break;
      case "--ttff-timeout":
        config.ttffTimeoutMs = parseInt(args[++i], 10);
        break;
      case "--warmup":
        config.warmup = true;
        break;
      case "--warmup-timeout":
        config.warmupTimeoutMs = parseInt(args[++i], 10);
        break;
      case "--help":
      case "-h":
        console.log(`
Multi-Client QoE Benchmark for WebRTC

Usage: npx tsx webrtc/client/qoe-benchmark.ts [options]

Options:
  --clients, -c <n>     Number of concurrent clients (default: 1)
  --duration, -d <sec>  Test duration in seconds (default: 60)
  --page, -p <url>      WebRTC page URL (default: http://localhost:8080/)
  --output, -o <dir>    Output directory for CSV files (default: ./qoe-results)
  --stagger <ms>        Delay between launching clients (default: 0)
  --browsers <n>        Number of Chromium processes to launch (default: 1)
  --ttff-timeout <ms>   Mark a client as failed if TTFF not reached (default: 10000)
  --warmup              Run a warmup page per browser and discard its TTFF/metrics
  --warmup-timeout <ms> Timeout for warmup TTFF before moving on (default: 7000)
  --headed              Run browsers in headed mode (visible windows)
  --help, -h            Show this help message
`);
        process.exit(0);
    }
  }

  return config;
}
