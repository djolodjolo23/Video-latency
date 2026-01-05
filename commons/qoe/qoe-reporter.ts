import fs from 'node:fs';
import path from 'node:path';
import { ClientMetrics } from './qoe-types.js';

export interface ReportConfig {
  numClients: number;
  durationSec: number;
  outputDir: string;
}

interface AggregateStats {
  allTTFF: number[];
  allLatencies: number[];
  avgStallsPerClient: number;
  avgStallTimePerClient: number;
  totalStalls: number;
  totalStallTime: number;
  pctClientsWithStall: number;
  p50StallMsPerClient: number;
  p95StallMsPerClient: number;
  totalErrors: number;
}

export function generateReports(allMetrics: ClientMetrics[], config: ReportConfig): void {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const baseFilename = `qoe_${config.numClients}clients_${config.durationSec}s_${timestamp}`;

  const csvPath = path.join(config.outputDir, `${baseFilename}_detailed.csv`);
  const csvHeader = 'second,client_id,latency_ms,stall_count,total_stall_ms,buffer_ahead_sec,is_stalling\n';

  let csvContent = csvHeader;
  for (const metrics of allMetrics) {
    for (const sec of metrics.secondBySecond) {
      csvContent += `${sec.second},${metrics.clientId},${sec.latencyMs ?? ''},${sec.stallCount},${sec.totalStallMs.toFixed(0)},${sec.bufferAheadSec.toFixed(3)},${sec.isStalling}\n`;
    }
  }

  fs.writeFileSync(csvPath, csvContent);
  console.log(`  Detailed CSV: ${csvPath}`);

  const summaryPath = path.join(config.outputDir, `${baseFilename}_summary.csv`);
  const summaryHeader = 'client_id,ttff_ms,avg_latency_ms,min_latency_ms,max_latency_ms,stall_count,total_stall_ms,error_count\n';

  let summaryContent = summaryHeader;
  for (const m of allMetrics) {
    const latencies = m.latencySamples.filter(l => l !== null && !isNaN(l));
    const avgLatency = latencies.length > 0 
      ? latencies.reduce((a, b) => a + b, 0) / latencies.length 
      : null;
    const minLatency = latencies.length > 0 ? Math.min(...latencies) : null;
    const maxLatency = latencies.length > 0 ? Math.max(...latencies) : null;

    summaryContent += `${m.clientId},${m.ttffMs ?? ''},${avgLatency?.toFixed(1) ?? ''},${minLatency?.toFixed(1) ?? ''},${maxLatency?.toFixed(1) ?? ''},${m.stallCount},${m.totalStallDurationMs.toFixed(0)},${m.errors.length}\n`;
  }

  fs.writeFileSync(summaryPath, summaryContent);
  console.log(`  Summary CSV:  ${summaryPath}`);

  const agg = computeAggregates(allMetrics);
  writeAggregateRow(agg, config, timestamp);
}

function computeAggregates(allMetrics: ClientMetrics[]): AggregateStats {
  const allTTFF = allMetrics.map(m => m.ttffMs).filter((t): t is number => t !== null);
  const allLatencies = allMetrics.flatMap(m => m.latencySamples).filter(l => !isNaN(l));
  const clientCount = allMetrics.length || 1;
  const totalStalls = allMetrics.reduce((sum, m) => sum + m.stallCount, 0);
  const totalStallTime = allMetrics.reduce((sum, m) => sum + m.totalStallDurationMs, 0);
  const avgStallsPerClient = totalStalls / clientCount;
  const avgStallTimePerClient = totalStallTime / clientCount;
  const clientsWithStall = allMetrics.filter(m => m.stallCount > 0).length;
  const pctClientsWithStall = (clientsWithStall / clientCount) * 100;
  const stallMsPerClient = allMetrics.map(m => m.totalStallDurationMs).sort((a, b) => a - b);
  const p50StallMsPerClient = percentile(stallMsPerClient, 0.5);
  const p95StallMsPerClient = percentile(stallMsPerClient, 0.95);
  const totalErrors = allMetrics.reduce((sum, m) => sum + m.errors.length, 0);

  return {
    allTTFF,
    allLatencies,
    avgStallsPerClient,
    avgStallTimePerClient,
    totalStalls,
    totalStallTime,
    pctClientsWithStall,
    p50StallMsPerClient,
    p95StallMsPerClient,
    totalErrors,
  };
}

function writeAggregateRow(agg: AggregateStats, config: ReportConfig, timestamp: string): void {
  const aggregatePath = path.join(config.outputDir, 'aggregate_results.csv');
  const aggregateExists = fs.existsSync(aggregatePath);

  const avgTTFF = agg.allTTFF.length > 0 ? agg.allTTFF.reduce((a, b) => a + b, 0) / agg.allTTFF.length : 0;
  const avgLatency = agg.allLatencies.length > 0 ? agg.allLatencies.reduce((a, b) => a + b, 0) / agg.allLatencies.length : 0;
  const minLatency = agg.allLatencies.length > 0 ? Math.min(...agg.allLatencies) : 0;
  const maxLatency = agg.allLatencies.length > 0 ? Math.max(...agg.allLatencies) : 0;

  const aggregateRow = `${timestamp},${config.numClients},${config.durationSec},${avgTTFF.toFixed(1)},${avgLatency.toFixed(1)},${minLatency.toFixed(1)},${maxLatency.toFixed(1)},${agg.avgStallsPerClient.toFixed(2)},${agg.avgStallTimePerClient.toFixed(0)},${agg.totalStalls},${agg.totalStallTime.toFixed(0)},${agg.pctClientsWithStall.toFixed(1)},${agg.p50StallMsPerClient.toFixed(0)},${agg.p95StallMsPerClient.toFixed(0)},${agg.totalErrors}\n`;

  if (!aggregateExists) {
    fs.writeFileSync(
      aggregatePath,
      'timestamp,num_clients,duration_sec,avg_ttff_ms,avg_latency_ms,min_latency_ms,max_latency_ms,avg_stalls_per_client,avg_stall_ms_per_client,total_stalls,total_stall_ms,pct_clients_with_stall,p50_stall_ms_per_client,p95_stall_ms_per_client,total_errors\n' +
        aggregateRow
    );
  } else {
    fs.appendFileSync(aggregatePath, aggregateRow);
  }
  console.log(`  Aggregate CSV: ${aggregatePath}`);
}

function percentile(values: number[], p: number): number {
  if (!values.length) return 0;
  const rank = Math.max(0, Math.min(values.length - 1, (values.length - 1) * p));
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) return values[lower];
  const weight = rank - lower;
  return values[lower] * (1 - weight) + values[upper] * weight;
}
