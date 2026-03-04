/**
 * External embedding indexer for qmd.
 *
 * Scans .md files in the vault, chunks them, embeds via emb-service HTTP API,
 * and writes vectors to PostgreSQL pgvector.
 *
 * Usage: node embed-index.js [--force]
 *   --force: re-embed all documents (ignore existing embeddings)
 */

import postgres from "postgres";
import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import { join, relative } from "node:path";

const EMB_URL = process.env.EMB_SERVICE_URL || "http://emb-service:3006";
const PG_URL = process.env.PGVECTOR_URL || "postgres://qmd:qmd@pgvector:5432/qmd_vectors";
const VAULT_PATH = process.env.QMD_VAULT_PATH || "/vault";
const BATCH_SIZE = 8;
const CHUNK_SIZE = 800; // approximate words per chunk
const CHUNK_OVERLAP = 120; // overlap words

const forceReindex = process.argv.includes("--force");

// ── Helpers ──────────────────────────────────────────────────────────────────

async function postJSON(url, body) {
  const payload = JSON.stringify(body);
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": String(Buffer.byteLength(payload)),
    },
    body: payload,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${url} returned ${res.status}: ${text}`);
  }
  return res.json();
}

async function embedBatch(texts) {
  const resp = await postJSON(`${EMB_URL}/v1/embeddings`, { input: texts });
  return resp.data.map((d) => d.embedding);
}

/**
 * Simple word-boundary chunking.
 * Splits text into chunks of ~CHUNK_SIZE words with CHUNK_OVERLAP word overlap.
 */
function chunkText(text) {
  const words = text.split(/\s+/);
  if (words.length <= CHUNK_SIZE) return [text];

  const chunks = [];
  let start = 0;
  while (start < words.length) {
    const end = Math.min(start + CHUNK_SIZE, words.length);
    chunks.push(words.slice(start, end).join(" "));
    if (end >= words.length) break; // reached the end
    start = end - CHUNK_OVERLAP;
  }
  return chunks;
}

/** Recursively find all .md files under dir, return [{path, hash, _fullPath}].
 *  Uses file stat (size + mtime) for hash to avoid reading all files into memory. */
async function discoverMarkdownFiles(dir) {
  const results = [];
  async function walk(d) {
    const entries = await readdir(d, { withFileTypes: true });
    for (const e of entries) {
      const full = join(d, e.name);
      if (e.isDirectory()) {
        // Skip hidden dirs (.obsidian, .claude-note, .trash)
        if (!e.name.startsWith(".")) await walk(full);
      } else if (e.name.endsWith(".md")) {
        const relPath = relative(dir, full);
        const st = await stat(full);
        // Hash based on path + size + mtime — avoids reading file content
        const hash = createHash("sha256")
          .update(`${relPath}:${st.size}:${st.mtimeMs}`)
          .digest("hex").slice(0, 16);
        results.push({ path: relPath, hash, _fullPath: full });
      }
    }
  }
  await walk(dir);
  return results;
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  console.log("==> embed-index: starting");
  console.log(`    emb-service: ${EMB_URL}`);
  console.log(`    pgvector: ${PG_URL.replace(/\/\/.*@/, "//***@")}`);
  console.log(`    batch size: ${BATCH_SIZE}`);

  // Wait for emb-service to be available
  let ready = false;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`${EMB_URL}/health`, { signal: AbortSignal.timeout(2000) });
      if (res.ok) { ready = true; break; }
    } catch {}
    console.log(`    waiting for emb-service... (${i + 1}/30)`);
    await new Promise((r) => setTimeout(r, 2000));
  }
  if (!ready) {
    console.error("==> embed-index: emb-service not available, skipping vector indexing");
    process.exit(1);
  }

  // Connect to PostgreSQL
  const sql = postgres(PG_URL, { max: 5, idle_timeout: 30 });

  // Verify pgvector is ready
  try {
    await sql`SELECT 1`;
    console.log("    pgvector: connected");
  } catch (err) {
    console.error("==> embed-index: pgvector not available:", err.message);
    await sql.end();
    process.exit(1);
  }

  // Discover documents by scanning the vault filesystem directly.
  // qmd's createStore() doesn't expose its internal SQLite handle, and
  // searchFTS("*") is invalid FTS5 syntax, so we scan .md files instead.
  let documents;
  try {
    documents = await discoverMarkdownFiles(VAULT_PATH);
  } catch (err) {
    console.error("==> embed-index: failed to scan vault:", err.message);
    await sql.end();
    process.exit(1);
  }

  console.log(`==> embed-index: found ${documents.length} documents`);

  // Determine which documents need embedding
  const existingHashes = new Set();
  if (!forceReindex) {
    const rows = await sql`SELECT DISTINCT doc_hash FROM doc_chunks`;
    rows.forEach((r) => existingHashes.add(r.doc_hash));
  }

  const toEmbed = documents.filter((d) => !existingHashes.has(d.hash));
  console.log(`==> embed-index: ${toEmbed.length} documents need embedding (${existingHashes.size} already done)`);

  if (toEmbed.length === 0) {
    console.log("==> embed-index: nothing to do");
    await sql.end();
    process.exit(0);
  }

  let totalChunks = 0;
  let processedDocs = 0;
  const pendingChunks = []; // {doc_path, doc_hash, chunk_index, chunk_text, text_for_embed}

  for (const doc of toEmbed) {
    // Read one document at a time to avoid OOM
    let body;
    try {
      body = await readFile(doc._fullPath, "utf8");
      if (!body) continue;
    } catch {
      continue;
    }

    // Extract title from first # heading or filename
    const titleMatch = body.match(/^#\s+(.+)$/m);
    const title = titleMatch ? titleMatch[1].trim() : doc.path.replace(/\.md$/, "").split("/").pop();
    const prefix = title ? `title: ${title} | text: ` : "text: ";
    const chunks = chunkText(body);

    chunks.forEach((chunk, idx) => {
      pendingChunks.push({
        doc_path: doc.path,
        doc_hash: doc.hash,
        chunk_index: idx,
        chunk_text: chunk,
        text_for_embed: prefix + chunk,
      });
    });

    processedDocs++;

    // Flush batch when large enough
    if (pendingChunks.length >= BATCH_SIZE) {
      await flushBatch(pendingChunks.splice(0, BATCH_SIZE));
    }
  }

  // Flush remaining
  while (pendingChunks.length > 0) {
    await flushBatch(pendingChunks.splice(0, BATCH_SIZE));
  }

  async function flushBatch(batch) {
    const texts = batch.map((c) => c.text_for_embed);
    let embeddings;
    try {
      embeddings = await embedBatch(texts);
    } catch (err) {
      console.error(`    batch embed failed: ${err.message}${err.cause ? ` (cause: ${err.cause.message || err.cause})` : ""}`);
      return;
    }

    // Build rows for bulk insert
    const rows = batch.map((c, i) => ({
      doc_path: c.doc_path,
      doc_hash: c.doc_hash,
      chunk_index: c.chunk_index,
      chunk_text: c.chunk_text,
      embedding: `[${embeddings[i].join(",")}]`,
    }));

    await sql`
      INSERT INTO doc_chunks ${sql(rows, "doc_path", "doc_hash", "chunk_index", "chunk_text", "embedding")}
      ON CONFLICT (doc_hash, chunk_index)
      DO UPDATE SET
        doc_path = EXCLUDED.doc_path,
        chunk_text = EXCLUDED.chunk_text,
        embedding = EXCLUDED.embedding,
        created_at = now()
    `;

    totalChunks += batch.length;
    process.stdout.write(`\r    embedded ${totalChunks} chunks from ${processedDocs}/${toEmbed.length} docs`);
  }

  // Clean up stale chunks for documents no longer in the vault
  const allHashes = new Set(documents.map((d) => d.hash));
  const pgHashes = await sql`SELECT DISTINCT doc_hash FROM doc_chunks`;
  const staleHashes = pgHashes.filter((r) => !allHashes.has(r.doc_hash)).map((r) => r.doc_hash);
  if (staleHashes.length > 0) {
    await sql`DELETE FROM doc_chunks WHERE doc_hash IN ${sql(staleHashes)}`;
    console.log(`\n    cleaned up ${staleHashes.length} stale document(s)`);
  }

  console.log(`\n==> embed-index: done — ${totalChunks} chunks from ${processedDocs} documents`);
  await sql.end();
}

main().catch(async (err) => {
  console.error("==> embed-index: fatal error:", err);
  process.exit(1);
});
