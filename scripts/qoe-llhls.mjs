#!/usr/bin/env node
const args = process.argv.slice(2);
const clientsArg = args[0];
const clients = clientsArg ? Number.parseInt(clientsArg, 10) : 1;

if (!Number.isFinite(clients) || clients <= 0) {
  console.error("Usage: npm run qoe:llhls -- <clients>");
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
  "30",
  "--system-metrics",
  "--stream",
  "http://localhost:8080/live.m3u8",
];

console.log(cmd.join(" "));
const { spawnSync } = await import("node:child_process");
const result = spawnSync(cmd[0], cmd.slice(1), { stdio: "inherit" });
process.exit(result.status ?? 1);
