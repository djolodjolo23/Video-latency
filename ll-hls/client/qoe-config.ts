import path from 'node:path';
import { fileURLToPath } from 'node:url';

export interface Config {
  numClients: number;
  durationSec: number;
  streamUrl: string;
  outputDir: string;
  headless: boolean;
  staggerDelayMs: number;
  numBrowsers: number;
  ttffTimeoutMs: number;
  warmup: boolean;
  warmupTimeoutMs: number;
  systemMetrics: boolean;
  systemMetricsIntervalSec: number;
  systemMetricsOutput: string;
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export function parseArgs(): Config {
  const args = process.argv.slice(2);
  const config: Config = {
    numClients: 1,
    durationSec: 60,
    streamUrl: 'http://localhost:8080/live.m3u8',
    outputDir: path.join(__dirname, 'qoe-results'),
    headless: true,
    staggerDelayMs: 0,
    numBrowsers: 1,
    ttffTimeoutMs: 10000,
    warmup: false,
    warmupTimeoutMs: 7000,
    systemMetrics: false,
    systemMetricsIntervalSec: 1,
    systemMetricsOutput: path.join(__dirname, 'qoe-results', 'system_metrics.csv'),
  };

  let outputOverridden = false;
  let systemMetricsOutputOverridden = false;
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--clients':
      case '-c':
        config.numClients = parseInt(args[++i], 10);
        break;
      case '--duration':
      case '-d':
        config.durationSec = parseInt(args[++i], 10);
        break;
      case '--stream':
      case '-s':
        config.streamUrl = args[++i];
        break;
      case '--output':
      case '-o':
        config.outputDir = args[++i];
        outputOverridden = true;
        break;
      case '--headed':
        config.headless = false;
        break;
      case '--browsers':
        config.numBrowsers = parseInt(args[++i], 10);
        break;
      case '--ttff-timeout':
        config.ttffTimeoutMs = parseInt(args[++i], 10);
        break;
      case '--warmup':
        config.warmup = true;
        break;
      case '--warmup-timeout':
        config.warmupTimeoutMs = parseInt(args[++i], 10);
        break;
      case '--system-metrics':
        config.systemMetrics = true;
        break;
      case '--system-metrics-interval':
        config.systemMetricsIntervalSec = parseFloat(args[++i]);
        break;
      case '--system-metrics-output':
        config.systemMetricsOutput = args[++i];
        systemMetricsOutputOverridden = true;
        break;
      case '--help':
      case '-h':
        console.log(`
Multi-Client QoE Benchmark for LL-HLS

Usage: npx tsx qoe-benchmark.ts [options]

Options:
  --clients, -c <n>     Number of concurrent clients (default: 1)
  --duration, -d <sec>  Test duration in seconds (default: 60)
  --stream, -s <url>    HLS stream URL (default: http://localhost:8080/live.m3u8)
  --output, -o <dir>    Output directory for CSV files (default: ./qoe-results)
  --browsers <n>        Number of Chromium processes to launch (default: 1)
  --ttff-timeout <ms>   Mark a client as failed if TTFF not reached (default: 10000)
  --warmup              Run a warmup page per browser and discard its TTFF/metrics
  --warmup-timeout <ms> Timeout for warmup TTFF before moving on (default: 7000)
  --system-metrics       Write periodic system CPU/mem to CSV (default: off)
  --system-metrics-interval <sec> Sampling interval (default: 1.0)
  --system-metrics-output <path> Output CSV path (default: <output>/system_metrics.csv)
  --headed              Run browsers in headed mode (visible windows)
  --help, -h            Show this help message
`);
        process.exit(0);
    }
  }

  if (outputOverridden && !systemMetricsOutputOverridden) {
    config.systemMetricsOutput = path.join(config.outputDir, 'system_metrics.csv');
  }

  return config;
}
