#!/usr/bin/env node
const args = process.argv.slice(2);
const numericArgs = [];
let durationArg = null;
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--duration" || args[i] === "-d") {
    durationArg = args[i + 1] ?? null;
    i += 1;
    continue;
  }
  if (!args[i].startsWith("-")) {
    numericArgs.push(args[i]);
  }
}
const startArg = numericArgs[0];
const endArg = numericArgs[1];
const start = startArg ? Number.parseInt(startArg, 10) : 14;
const end = endArg ? Number.parseInt(endArg, 10) : 30;

if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0 || end < start) {
  console.error("Usage: npm run qoe:webrtc:range -- <start> <end> [--duration <sec>]");
  process.exit(1);
}

const baseSec = Number.parseInt(process.env.QOE_BASE_SEC || "10", 10);
const durationSec = Number.parseInt(durationArg || process.env.QOE_DURATION_SEC || "30", 10);
const perClientSec = Number.parseFloat(process.env.QOE_CLIENT_SEC || "2");
const fixedTimeoutSec = process.env.QOE_TIMEOUT_SEC
  ? Number.parseInt(process.env.QOE_TIMEOUT_SEC, 10)
  : null;
const graceSec = Number.parseInt(process.env.QOE_GRACE_SEC || "10", 10);

const { spawn } = await import("node:child_process");

let currentChild = null;
let isShuttingDown = false;

async function killAllChrome() {
  try {
    const { execSync } = await import("node:child_process");
    console.log("\nKilling remaining Chrome/Chromium processes...");
    execSync("pkill -f 'Google Chrome|Chromium|chrome' || true", { stdio: "inherit" });
  } catch (err) {
    // ignore errors from pkill
  }
}

async function cleanupChild() {
  if (currentChild && !currentChild.killed) {
    try {
      currentChild.kill("SIGTERM");
      // Give it a moment to shut down gracefully
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!currentChild.killed) {
        currentChild.kill("SIGKILL");
      }
    } catch {
      // ignore
    }
  }
}

const handleSignal = async (signal) => {
  if (isShuttingDown) return;
  isShuttingDown = true;
  console.log(`\nReceived ${signal}. Cleaning up...`);
  await cleanupChild();
  await killAllChrome();
  process.exit(130);
};

process.on("SIGINT", () => handleSignal("SIGINT"));
process.on("SIGTERM", () => handleSignal("SIGTERM"));

async function runOnce(clients) {
  return new Promise((resolve) => {
    const child = spawn(
      "node",
      ["scripts/qoe-webrtc.mjs", String(clients), "--duration", String(durationSec)],
      { stdio: "inherit" }
    );
    currentChild = child;
    let timeout = null;
    let graceTimeout = null;
    let killed = false;
    const computedTimeoutSec = fixedTimeoutSec ?? Math.ceil(durationSec + baseSec + perClientSec * clients);
    if (Number.isFinite(computedTimeoutSec) && computedTimeoutSec > 0) {
      timeout = setTimeout(() => {
        console.log(
          `\nTimeout after ${computedTimeoutSec}s for ${clients} clients. Sending SIGINT...`
        );
        if (child.exitCode === null) {
          child.kill("SIGINT");
        }
        graceTimeout = setTimeout(() => {
          if (child.exitCode === null) {
            console.log("Process still running; sending SIGKILL...");
            killed = true;
            child.kill("SIGKILL");
          }
        }, Math.max(graceSec, 1) * 1000);
      }, computedTimeoutSec * 1000);
    }
    child.on("exit", async (code, signal) => {
      if (timeout) clearTimeout(timeout);
      if (graceTimeout) clearTimeout(graceTimeout);
      currentChild = null;
      // Always try to kill any remaining Chrome processes between runs
      await killAllChrome();
      resolve({ code, signal, killed });
    });
  });
}

try {
  for (let clients = start; clients <= end; clients += 1) {
    console.log(`\n=== Running WebRTC QoE for ${clients} clients ===`);
    const result = await runOnce(clients);
    const interrupted = result.killed || result.signal === "SIGINT";
    if (result.code !== 0 && !interrupted) {
      console.error(`Run failed for ${clients} clients (code ${result.code ?? "?"}). Stopping.`);
      await killAllChrome();
      process.exit(result.code ?? 1);
    }
    if (clients < end) {
      const pauseMs = 30000;
      console.log(`Waiting ${pauseMs / 1000}s before next run...`);
      await new Promise((resolve) => setTimeout(resolve, pauseMs));
    }
  }
  console.log("\n=== All runs complete ===");
  await killAllChrome();
  process.exit(0);
} catch (err) {
  console.error("Script error:", err);
  await killAllChrome();
  process.exit(1);
}
