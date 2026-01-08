#!/usr/bin/env node
const args = process.argv.slice(2);
const clientsArg = args[0];
const clients = clientsArg ? Number.parseInt(clientsArg, 10) : 1;
let durationSec = 30;

for (let i = 1; i < args.length; i += 1) {
  if (args[i] === "--duration" || args[i] === "-d") {
    durationSec = Number.parseInt(args[i + 1], 10);
    i += 1;
  }
}

if (!Number.isFinite(clients) || clients <= 0 || !Number.isFinite(durationSec) || durationSec <= 0) {
  console.error("Usage: npm run qoe:llhls -- <clients> [--duration <sec>]");
  process.exit(1);
}

const cmd = [
  "npx",
  "tsx",
  "ll-hls/client/qoe-benchmark.ts",
  "--clients",
  String(clients),
  "--browsers",
  String(clients),
  "--duration",
  String(durationSec),
  "--system-metrics",
  "--stream",
  "http://192.168.0.93:8080/live.m3u8",
];

console.log(cmd.join(" "));
const { spawnSync } = await import("node:child_process");
const result = spawnSync(cmd[0], cmd.slice(1), { stdio: "inherit" });
process.exit(result.status ?? 1);
