// claude-note recall server: QMD's search, kept warm.
//
// A recall hook runs on every prompt. Starting QMD per search meant ~1.5 s per process and up to a
// dozen processes; loading the embedding model alone takes seconds. This keeps one QMD store and its
// models loaded and answers on 127.0.0.1 only. It fetches; claude_note/recall.py fuses, gates and
// presents (so the ranking logic stays in one tested place).
//
//   node recall_server.mjs <qmd-sdk-index.js> [port=8182] [index-db] [index-yml]
//   GET  /health                    -> {ok, uptime_s, loaded_ms}
//   POST /search {rungs: [str], question: str, lex_limit: 20, vec_limit: 40}
//        -> {lex: [[{file, title, score}]], vec: [{file, title, score}], ms: {lex, vec}}
//   POST /rerank {question, files: [str], chars: 2000}  -> {scores: {file: score}, ms}
//
// Requests are served one at a time (node-llama-cpp contexts are not safe to share concurrently).
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';

const [sdkPath, portArg, dbArg, ymlArg] = process.argv.slice(2);
if (!sdkPath) { console.error('usage: node recall_server.mjs <qmd-sdk-index.js> [port] [db] [yml]'); process.exit(2); }
const port = Number(portArg || 8182);
const sdk = await import(pathToFileURL(sdkPath).href);
const llmMod = await import(pathToFileURL(join(dirname(sdkPath), 'llm.js')).href);
const cache = process.env.XDG_CACHE_HOME || join(homedir(), '.cache');
const store = await sdk.createStore({
  dbPath: dbArg || join(cache, 'qmd', 'index.sqlite'),
  configPath: ymlArg || join(process.env.QMD_CONFIG_DIR || join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'qmd'), 'index.yml'),
});
const llm = llmMod.getDefaultLlamaCpp();
const started = Date.now();
// Warm the embedding model before the first prompt pays for it.
const t0 = performance.now();
await store.searchVector('warm up', { limit: 1 }).catch(() => []);
const loadedMs = Math.round(performance.now() - t0);

const pick = (r) => ({ file: r.filepath ?? r.file, title: r.title, score: Math.round((r.score ?? 0) * 10000) / 10000,
  snippet: String(r.snippet || r.body || '').slice(0, 600) });
let queue = Promise.resolve();
const serial = (fn) => (queue = queue.then(fn, fn));

async function search(body) {
  const t = performance.now();
  const lex = [];
  for (const rung of (body.rungs || []).slice(0, 8)) lex.push((await store.searchLex(String(rung), { limit: body.lex_limit || 20 })).map(pick));
  const tl = performance.now();
  const vec = body.question ? (await store.searchVector(String(body.question).slice(0, 2000), { limit: body.vec_limit || 40 })).map(pick) : [];
  return { lex, vec, ms: { lex: Math.round(tl - t), vec: Math.round(performance.now() - tl) } };
}

async function rerank(body) {
  const t = performance.now();
  const docs = [];
  for (const f of (body.files || []).slice(0, 20)) {
    const d = await store.get(String(f), { includeBody: true }).catch(() => null);
    if (d) docs.push({ file: String(f), title: d.title || '', text: String(d.body || '').slice(0, body.chars || 2000) });
  }
  const r = docs.length ? await llm.rerank(String(body.question || ''), docs) : { results: [] };
  const scores = {};
  for (const x of r.results || []) scores[x.file] = Math.round(x.score * 10000) / 10000;
  return { scores, ms: Math.round(performance.now() - t) };
}

const server = http.createServer((req, res) => {
  const send = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
  if (req.method === 'GET' && req.url === '/health') return send(200, { ok: true, uptime_s: Math.round((Date.now() - started) / 1000), loaded_ms: loadedMs });
  if (req.method !== 'POST' || !['/search', '/rerank'].includes(req.url)) return send(404, { error: 'not found' });
  let raw = '';
  req.on('data', (c) => { raw += c; if (raw.length > 1e6) req.destroy(); });
  req.on('end', () => serial(async () => {
    try {
      const body = JSON.parse(raw || '{}');
      send(200, req.url === '/search' ? await search(body) : await rerank(body));
    } catch (e) {
      send(500, { error: String(e && e.message || e).slice(0, 300) });
    }
  }));
});
server.listen(port, '127.0.0.1', () => console.error(`claude-note recall server on 127.0.0.1:${port} (models warm in ${loadedMs} ms)`));
