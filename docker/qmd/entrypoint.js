/**
 * Entrypoint for qmd container (replaces entrypoint.sh for distroless).
 * 1. Run embed-index.js for initial indexing
 * 2. Start background reindex loop
 * 3. Start HTTP server
 */

import { fork } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const VAULT_PATH = process.env.QMD_VAULT_PATH || "/vault";
const REINDEX_INTERVAL = parseInt(process.env.QMD_REINDEX_INTERVAL || "300", 10) * 1000;
const PORT = process.env.QMD_PORT || "8686";

console.log(`==> qmd entrypoint: vault=${VAULT_PATH}  port=${PORT}  reindex=${REINDEX_INTERVAL / 1000}s`);

function runEmbedIndex() {
  return new Promise((resolve) => {
    const child = fork(join(__dirname, "embed-index.js"));
    child.on("exit", (code) => resolve(code || 0));
    child.on("error", () => resolve(1));
  });
}

// ── Phase 1: Initial embedding ──────────────────────────────────────────────

console.log("==> Embedding documents via emb-service...");
const code = await runEmbedIndex();
if (code !== 0) console.log("==> Warning: vector indexing failed (emb-service may not be ready yet)");

// ── Phase 2: Background reindex loop ────────────────────────────────────────

setInterval(async () => {
  console.log("==> Re-indexing vault...");
  await runEmbedIndex();
  console.log("==> Re-index complete.");
}, REINDEX_INTERVAL);

// ── Phase 3: Start HTTP server ──────────────────────────────────────────────

console.log(`==> Starting qmd-server on :${PORT}`);
await import("./server.js");
