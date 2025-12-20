export interface QoEEvent {
  type: string;
  clientId: string;
  timestamp: number;
  elapsedMs: number;
  [key: string]: any;
}

export interface ClientMetrics {
  clientId: string;
  ttffMs: number | null;
  latencySamples: number[];
  stallCount: number;
  totalStallDurationMs: number;
  errors: string[];
  secondBySecond: SecondMetrics[];
}

export interface SecondMetrics {
  second: number;
  latencyMs: number | null;
  stallCount: number;
  totalStallMs: number;
  bufferAheadSec: number;
  isStalling: boolean;
}