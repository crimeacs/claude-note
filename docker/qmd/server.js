/**
 * REST search server — PostgreSQL FTS + pgvector + external reranking.
 *
 * Model-free: all embedding and reranking is delegated to external services
 * (emb-service and rerank-service). This server handles FTS (PostgreSQL tsvector),
 * vector search (PostgreSQL pgvector with externally-computed embeddings), and
 * hybrid orchestration (FTS + vector + rerank via RRF).
 */

import http from "node:http";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { execFile } from "node:child_process";
import postgres from "postgres";

const PORT = parseInt(process.env.QMD_PORT || "8686", 10);
const REQUEST_TIMEOUT = 60_000;
const VAULT_PATH = process.env.QMD_VAULT_PATH || "/vault";

const EMB_URL = process.env.EMB_SERVICE_URL || "http://emb-service:3006";
const RERANK_URL = process.env.RERANK_SERVICE_URL || "http://rerank-service:3007";
const PG_URL = process.env.PGVECTOR_URL || "postgres://qmd:qmd@pgvector:5432/qmd_vectors";
const sql = postgres(PG_URL, { max: 10, idle_timeout: 60 });

// ── External service helpers ─────────────────────────────────────────────────

/** POST JSON to a URL, return parsed response. */
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

/** Embed a query string via emb-service. Returns float[] vector. */
async function embedQuery(text) {
  const resp = await postJSON(`${EMB_URL}/v1/embeddings`, { input: text });
  return resp.data[0].embedding;
}

/**
 * Rerank documents via rerank-service.
 * Returns [{index, relevance_score}, ...] sorted by score descending.
 */
async function rerankDocuments(query, documents, topN, timeoutMs = 2000) {
  const body = { query, documents };
  if (topN != null) body.top_n = topN;
  const payload = JSON.stringify(body);
  const res = await fetch(`${RERANK_URL}/v1/rerank`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": String(Buffer.byteLength(payload)),
    },
    body: payload,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`rerank returned ${res.status}: ${text}`);
  }
  const resp = await res.json();
  return resp.results;
}

/** Check if an external service is healthy. */
async function checkHealth(url) {
  try {
    const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return { ok: false, error: `status ${res.status}` };
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// ── PostgreSQL FTS search ────────────────────────────────────────────────────

/**
 * Full-text search using PostgreSQL tsvector + websearch_to_tsquery.
 * Returns best chunk per document, sorted by ts_rank_cd score.
 */
async function ftsSearch(query, limit = 10) {
  const results = await sql`
    SELECT DISTINCT ON (doc_path)
           doc_path AS path, chunk_text AS snippet,
           ts_rank_cd(fts, websearch_to_tsquery('english', ${query}), 5) AS score
    FROM doc_chunks
    WHERE fts @@ websearch_to_tsquery('english', ${query})
    ORDER BY doc_path, score DESC
  `;
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, limit).map(r => ({
    path: r.path,
    snippet: r.snippet,
    score: Number(r.score),
    source: "fts",
  }));
}

// ── Vector search ────────────────────────────────────────────────────────────

/** Vector search using external embeddings + PostgreSQL pgvector. Per-doc dedup. */
async function vectorSearch(query, limit = 10, embedding = null) {
  if (!embedding) embedding = await embedQuery(query);
  const vecStr = `[${embedding.join(",")}]`;
  const results = await sql`
    SELECT DISTINCT ON (doc_path)
           doc_path AS path, chunk_text AS snippet,
           1 - (embedding <=> ${vecStr}::vector) AS score
    FROM doc_chunks
    ORDER BY doc_path, embedding <=> ${vecStr}::vector
    LIMIT ${limit * 3}
  `;
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, limit).map(r => ({
    path: r.path,
    snippet: r.snippet,
    score: Number(r.score),
    source: "vector",
  }));
}

// ── Hybrid search with RRF + external reranking ──────────────────────────────

const RRF_K = 60; // Reciprocal Rank Fusion constant

/**
 * RRF merge + rerank: combines pre-computed FTS and vector results.
 * Used by both hybridSearch and the /compare endpoint to avoid duplicate queries.
 */
async function rrfMergeAndRerank(query, limit, ftsResults, vectorResults) {
  const candidateCount = Math.max(limit * 2, 15);

  // RRF merge
  const scores = new Map(); // path → { rrfScore, result }

  ftsResults.forEach((r, rank) => {
    const existing = scores.get(r.path) || { rrfScore: 0, result: r };
    existing.rrfScore += 1 / (RRF_K + rank + 1);
    scores.set(r.path, existing);
  });

  vectorResults.forEach((r, rank) => {
    const existing = scores.get(r.path) || { rrfScore: 0, result: r };
    existing.rrfScore += 1 / (RRF_K + rank + 1);
    scores.set(r.path, existing);
  });

  const merged = [...scores.values()]
    .sort((a, b) => b.rrfScore - a.rrfScore)
    .slice(0, candidateCount);

  const candidates = merged.map((m) => ({
    ...m.result,
    rrfScore: m.rrfScore,
  }));

  // Truncate snippets to ~350 words — cross-encoders have max 512 tokens
  const MAX_RERANK_WORDS = 350;
  const snippets = candidates.map((c) => {
    const s = c.snippet;
    if (!s) return "";
    const words = s.split(/\s+/);
    return words.length > MAX_RERANK_WORDS ? words.slice(0, MAX_RERANK_WORDS).join(" ") : s;
  }).filter(Boolean);

  // Rerank if we have snippets and the service is available
  if (snippets.length > 0) {
    try {
      const reranked = await rerankDocuments(query, snippets, limit);
      return reranked.map((r) => ({
        ...candidates[r.index],
        score: r.relevance_score,
        source: "hybrid",
      }));
    } catch (err) {
      console.warn("Reranking failed, falling back to RRF order:", err.message);
    }
  }

  // Fallback: return RRF-ordered results without reranking
  return candidates.slice(0, limit).map((c) => ({
    ...c,
    score: c.rrfScore,
    source: "hybrid",
  }));
}

/**
 * Hybrid search: FTS + vector + RRF + rerank.
 */
async function hybridSearch(query, limit = 10, embedding = null) {
  if (!embedding) embedding = await embedQuery(query);
  const candidateCount = Math.max(limit * 2, 15);

  const [ftsResults, vectorResults] = await Promise.all([
    ftsSearch(query, candidateCount),
    vectorSearch(query, candidateCount, embedding),
  ]);

  return rrfMergeAndRerank(query, limit, ftsResults, vectorResults);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Run an external command. */
function run(cmd, args, timeout = REQUEST_TIMEOUT) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout }, (err, stdout, stderr) => {
      resolve({
        stdout: stdout || "",
        stderr: stderr || "",
        code: err ? err.code || 1 : 0,
      });
    });
  });
}

/** Parse JSON body from an IncomingMessage. */
function parseBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString()));
      } catch {
        resolve({});
      }
    });
    req.on("error", reject);
  });
}

/** Send JSON response. */
function json(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

/**
 * Normalize a search result to include `path` and `snippet` fields
 * that Python clients expect.
 */
function normalizeResult(r) {
  return {
    ...r,
    path: r.path || "",
    snippet: r.snippet || "",
  };
}

function normalizeResults(results) {
  return (results || []).map(normalizeResult);
}

// ── Route table ──────────────────────────────────────────────────────────────

const routes = {};

// GET /health — includes external service status
routes["GET /health"] = async () => {
  try {
    const [stats, emb, rerank, pg] = await Promise.all([
      sql`SELECT count(*)::int AS chunks, count(DISTINCT doc_path)::int AS documents FROM doc_chunks`,
      checkHealth(EMB_URL),
      checkHealth(RERANK_URL),
      sql`SELECT 1`.then(() => ({ ok: true })).catch(e => ({ ok: false, error: e.message })),
    ]);
    const { chunks, documents } = stats[0];
    return {
      status: 200,
      data: {
        ok: true,
        documents,
        chunks,
        services: { emb_service: emb, rerank_service: rerank, pgvector: pg },
      },
    };
  } catch (err) {
    return { status: 503, data: { ok: false, error: err.message } };
  }
};

// GET /status
routes["GET /status"] = async () => {
  try {
    const stats = await sql`SELECT count(*)::int AS chunks, count(DISTINCT doc_path)::int AS documents FROM doc_chunks`;
    return { status: 200, data: stats[0] };
  } catch (err) {
    return { status: 500, data: { error: err.message } };
  }
};

// POST /search  { query, limit? }  — FTS full-text search
routes["POST /search"] = async (req) => {
  const body = await parseBody(req);
  if (!body.query) {
    return { status: 400, data: { error: "missing 'query'" } };
  }
  try {
    const results = await ftsSearch(body.query, body.limit || 10);
    return { status: 200, data: { results: normalizeResults(results) } };
  } catch (err) {
    return { status: 500, data: { error: err.message } };
  }
};

// POST /vsearch  { query, limit? }  — vector search (external embedding)
routes["POST /vsearch"] = async (req) => {
  const body = await parseBody(req);
  if (!body.query) {
    return { status: 400, data: { error: "missing 'query'" } };
  }
  try {
    const results = await vectorSearch(body.query, body.limit || 10);
    return { status: 200, data: { results: normalizeResults(results) } };
  } catch (err) {
    return { status: 500, data: { error: err.message } };
  }
};

// POST /query  { query, limit? }  — hybrid: FTS + vector + rerank
routes["POST /query"] = async (req) => {
  const body = await parseBody(req);
  if (!body.query) {
    return { status: 400, data: { error: "missing 'query'" } };
  }
  try {
    const results = await hybridSearch(body.query, body.limit || 10);
    return { status: 200, data: { results: normalizeResults(results) } };
  } catch (err) {
    return { status: 500, data: { error: err.message } };
  }
};

// GET /document?path=...
routes["GET /document"] = async (_req, url) => {
  const docPath = url.searchParams.get("path");
  if (!docPath) {
    return { status: 400, data: { error: "missing 'path' query param" } };
  }
  try {
    const content = await readFile(join(VAULT_PATH, docPath), "utf8");
    return { status: 200, data: { path: docPath, content } };
  } catch (err) {
    if (err.code === "ENOENT") {
      return { status: 404, data: { error: "not found" } };
    }
    return { status: 500, data: { error: err.message } };
  }
};

// POST /reindex  — re-embed via embed-index.js
routes["POST /reindex"] = async () => {
  const embedResult = await run(
    "node", ["/opt/qmd/embed-index.js"],
    300_000,
  ).catch(() => ({ code: 1, stdout: "", stderr: "embed-index not available" }));
  return {
    status: embedResult.code === 0 ? 200 : 500,
    data: {
      embed: embedResult.code === 0 ? "ok" : embedResult.stderr,
    },
  };
};

// GET /compare?q=...  — run all 3 search modes side by side
routes["GET /compare"] = async (_req, url) => {
  const query = url.searchParams.get("q");
  if (!query) {
    return { status: 400, data: { error: "missing 'q' query param" } };
  }
  const limit = parseInt(url.searchParams.get("limit") || "5", 10);

  try {
    // Embed once, query FTS+vector once, share results across all three views
    const embedding = await embedQuery(query);
    const candidateCount = Math.max(limit * 2, 15);
    const [ftsAll, vectorAll] = await Promise.all([
      ftsSearch(query, candidateCount),
      vectorSearch(query, candidateCount, embedding),
    ]);
    const hybrid = await rrfMergeAndRerank(query, limit, ftsAll, vectorAll);

    return {
      status: 200,
      data: {
        query,
        fts: { results: normalizeResults(ftsAll.slice(0, limit)) },
        vector: { results: normalizeResults(vectorAll.slice(0, limit)) },
        hybrid: { results: normalizeResults(hybrid) },
      },
    };
  } catch (err) {
    return { status: 500, data: { error: err.message } };
  }
};

// ── Server ───────────────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const key = `${req.method} ${url.pathname}`;

  const handler = routes[key];
  if (!handler) {
    return json(res, 404, { error: "not found", path: url.pathname });
  }

  try {
    const { status, data } = await handler(req, url);
    json(res, status, data);
  } catch (err) {
    json(res, 500, { error: err.message });
  }
});

server.listen(PORT, () => {
  console.log(`qmd-server listening on :${PORT}`);
  console.log(`  vault: ${VAULT_PATH}`);
  console.log(`  emb-service: ${EMB_URL}`);
  console.log(`  rerank-service: ${RERANK_URL}`);
  console.log(`  pgvector: ${PG_URL.replace(/\/\/.*@/, "//***@")}`);
});

// Graceful shutdown — close PostgreSQL pool before exit
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    console.log(`\nReceived ${sig}, shutting down...`);
    server.close();
    await sql.end({ timeout: 5 });
    process.exit(0);
  });
}
