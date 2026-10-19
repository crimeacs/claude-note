// QMD 2.8.3 adapter. Lexical reads run in a disposable, read-only SQLite worker;
// native model calls stay serial. A timed-out model poisons this process until
// its supervisor restarts it: returning a timeout must never admit another model
// call while the first one still owns a llama.cpp context.
import http from 'node:http';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { join, dirname, basename, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { readFileSync, readdirSync, statSync, openSync, readSync, closeSync } from 'node:fs';
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';

const PIN = '2.8.3';
const digest = value => createHash('sha256').update(value).digest('hex');
const boundedInt = (value, fallback, max) => Math.max(1, Math.min(max, Number(value) || fallback));
const err = (code, message) => Object.assign(new Error(message), { code });
const collectionsOf = body => Array.isArray(body.collections)
  ? [...new Set(body.collections.filter(c => typeof c === 'string' && c.length > 0 && c.length <= 200))].slice(0, 40) : [];

// Rungs are literal content terms, not SQL/FTS operators. Quoting protects both
// identifiers and Unicode; the QMD tokenizer still supplies its own stemming.
export function literalFTS(query) {
  return (String(query).match(/[\p{L}\p{N}_]+/gu) || []).slice(0, 32)
    .map(term => /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(term)
      ? `"${term.replace(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]+/gu,
        run => ` ${Array.from(run).join(' ')} `).trim()}"`
      : `"${term.replaceAll('"', '""')}"*`).join(' AND ');
}

export function passage(text, question, chars = 260) {
  text = String(text || '');
  chars = boundedInt(chars, 260, 4000);
  const terms = new Set((String(question).toLocaleLowerCase().match(/[\p{L}\p{N}_]+/gu) || []).filter(t => t.length > 1));
  let offset = 0, frontmatter = text.startsWith('---\n'), best = null;
  const blocks = text.split(/(?<=\n)(?=\s*\n)/);
  for (const block of blocks) {
    const start = offset; offset += block.length;
    let raw = block, begin = start;
    if (frontmatter) {
      const end = text.indexOf('\n---', 4);
      if (end < 0 || offset <= end + 4) continue;
      frontmatter = false;
      begin = Math.max(start, end + 4); raw = text.slice(begin, offset);
    }
    // Machine metadata is never a readable evidence passage.
    raw = raw.replace(/^<!--[^\n]*-->[^\S\n]*$/gm, line => ' '.repeat(line.length));
    const leading = raw.match(/^\s*/)[0].length;
    begin += leading; raw = raw.slice(leading);
    for (let i = 0; i < raw.length; i += Math.max(1, Math.floor(chars * .75))) {
      const original = raw.slice(i, i + chars);
      const value = original.replace(/^#+\s*/gm, '').replace(/\s+/g, ' ').trim();
      if (value.length < 40) continue;
      const words = new Set(value.toLocaleLowerCase().match(/[\p{L}\p{N}_]+/gu) || []);
      const score = [...terms].filter(t => words.has(t)).reduce((n, t) => n + 1 + Math.min(t.length, 20) / 20, 0);
      if (!best || score > best.score || (score === best.score && begin + i > best.anchor.start_char)) {
        const a = begin + i, b = a + original.length;
        best = { text: value, score, anchor: { start_char: a, end_char: b,
          start_line: text.slice(0, a).split('\n').length, end_line: text.slice(0, b).split('\n').length } };
      }
    }
  }
  return best || { text: '', anchor: {} };
}

function pick(row, question, chars, lexical = false) {
  const file = String(row.filepath || row.file || '');
  const selected = passage(row.body || row.snippet || '', question, chars);
  const revision = row.revision || (row.body ? digest(String(row.body)) : row.hash || '');
  const source_id = row.source_id || file;
  return { file, title: String(row.title || ''), score: Number(row.score) || 0,
    source_id, revision, revision_kind: row.revision_kind || (row.body ? 'indexed_body_sha256' : 'qmd_content_sha256'),
    snippet: selected.text, scored_text: selected.text, anchor: selected.anchor,
    passage_id: digest(JSON.stringify([source_id, revision, selected.anchor, selected.text])), lexical_evidence: lexical,
    has_machine_claims: /<!-- claude-note:claim /.test(String(row.body || '')) };
}

// Validate exactly the schema whose semantics were checked against QMD v2.8.3.
// No HTTP body controls dbPath, SQLite module, schema, or SQL text.
function openReadOnly({ dbPath, sqliteModule, version }) {
  if (version !== PIN) throw err('unsupported_schema', 'Scoped adapter requires QMD 2.8.3');
  const Database = createRequire(import.meta.url)(sqliteModule);
  const db = new Database(dbPath, { readonly: true, fileMustExist: true, timeout: 100 });
  db.pragma('query_only = ON');
  for (const [table, required] of Object.entries({ documents: ['id', 'collection', 'path', 'title', 'hash', 'active'],
    content: ['hash', 'doc'], documents_fts: ['filepath', 'title', 'body'] })) {
    const columns = new Set(db.pragma(`table_info(${table})`).map(c => c.name));
    if (!required.every(c => columns.has(c))) { db.close(); throw err('unsupported_schema', `Unsupported ${table} schema`); }
  }
  const fts = db.prepare("SELECT sql FROM sqlite_master WHERE name='documents_fts'").get()?.sql || '';
  if (!/USING fts5/i.test(fts)) { db.close(); throw err('unsupported_schema', 'Expected FTS5'); }
  return db;
}

export function scopedLexical(db, query, collections, limit) {
  const match = literalFTS(query);
  if (!match) return [];
  const filter = collections.length ? `AND collection IN (${collections.map(() => '?').join(',')})` : '';
  // Materialize allowed IDs before rank/limit. This deliberately avoids QMD
  // 2.8.3's global limit*10 CTE, which can starve a small requested collection.
  const sql = `WITH allowed AS MATERIALIZED (
      SELECT id FROM documents WHERE active=1 ${filter}
    ), matches AS MATERIALIZED (
      SELECT rowid, bm25(documents_fts,1.5,4.0,1.0) AS rank FROM documents_fts
      WHERE documents_fts MATCH ? AND rowid IN (SELECT id FROM allowed)
      ORDER BY rank ASC, rowid ASC LIMIT ?
    ) SELECT 'qmd://' || d.collection || '/' || d.path AS filepath, d.title, d.hash,
      content.doc AS body, matches.rank FROM matches
      JOIN documents d ON d.id=matches.rowid JOIN content ON content.hash=d.hash
      ORDER BY matches.rank ASC, d.collection ASC, d.path ASC`;
  return db.prepare(sql).all(...collections, match, limit).map(row => ({ ...row,
    score: Math.abs(row.rank) / (1 + Math.abs(row.rank)) }));
}

function indexMetadata(db) {
  const hash = createHash('sha256'), scopes = new Map();
  for (const row of db.prepare('SELECT collection,path,hash FROM documents WHERE active=1 ORDER BY collection,path').iterate()) {
    hash.update(JSON.stringify(row) + '\n');
    scopes.set(row.collection, (scopes.get(row.collection) || 0) + 1);
  }
  let vectorRows = 0, vectorModels = [], scopedVectors = [];
  if (db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_vectors'").get()) {
    const rows = db.prepare('SELECT model,COUNT(*) AS n FROM content_vectors GROUP BY model ORDER BY model').all();
    vectorRows = rows.reduce((n, row) => n + row.n, 0); vectorModels = rows.map(row => row.model);
    scopedVectors = db.prepare(`SELECT d.collection,COUNT(*) AS rows FROM content_vectors cv
      JOIN documents d ON d.hash=cv.hash AND d.active=1 GROUP BY d.collection ORDER BY d.collection`).all();
  } // A lexical-only schema without content_vectors is supported.
  const corpus_digest = hash.digest('hex');
  return { corpus_digest, corpus_digest_kind: 'indexed_document_hashes', generation: digest(JSON.stringify([corpus_digest, vectorRows, vectorModels])),
    collections: [...scopes].map(([name, documents]) => ({ name, documents,
      vector_rows: scopedVectors.find(c => c.collection === name)?.rows || 0 })), vector_rows: vectorRows, vector_models: vectorModels };
}

export function createIndexSnapshotReader(db) {
  let cached;
  return (readRows = () => []) => {
    try {
      db.exec('BEGIN');
      // This read establishes the SQLite snapshot. data_version belongs to this
      // connection and changes for external commits, including vector-only
      // updates. Neither file mtimes nor corpus hashes cover that boundary.
      const version = db.pragma('data_version', { simple: true });
      const metadata = cached?.version === version ? cached.metadata : indexMetadata(db);
      const rows = readRows();
      db.exec('COMMIT');
      // A failed metadata/query/commit never publishes a cache entry. The next
      // transaction rechecks data_version before reusing a successful snapshot.
      cached = { version, metadata };
      return { rows, metadata };
    } catch (error) {
      try { db.exec('ROLLBACK'); } catch { /* closed connection */ }
      throw error;
    }
  };
}

if (!isMainThread && workerData?.kind === 'claude-note-scoped-fts') {
  let db;
  try { db = openReadOnly(workerData); }
  catch (e) { parentPort.postMessage({ error: e.code || 'adapter_unavailable' }); }
  const readSnapshot = db && createIndexSnapshotReader(db);
  if (db) parentPort.on('message', async msg => {
    try {
      const sdkStore = msg.kind === 'sdk_fallback' ? await import(pathToFileURL(workerData.sdkStorePath).href) : null;
      const snapshot = readSnapshot(() => msg.kind === 'search' ? scopedLexical(db, msg.query, msg.collections, msg.limit)
        : sdkStore ? sdkStore.searchFTS(db, msg.query, msg.limit, msg.collections.length ? msg.collections : undefined) : []);
      parentPort.postMessage({ id: msg.id, ...snapshot });
    } catch (e) {
      parentPort.postMessage({ id: msg.id, error: e.code || 'adapter_query_failed' });
    }
  });
}

if (!isMainThread && workerData?.kind === 'claude-note-model-identity') {
  let fd;
  try {
    fd = openSync(workerData.path, 'r');
    const hash = createHash('sha256'), buffer = Buffer.alloc(1024 * 1024);
    let count;
    while ((count = readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, count));
    parentPort.postMessage({ revision: hash.digest('hex'), revision_kind: 'model_file_sha256' });
  } catch { parentPort.postMessage({ revision: 'unknown', revision_kind: 'unknown' }); }
  finally { if (fd !== undefined) closeSync(fd); }
}

export async function modelFileIdentity(path, deadline = Date.now() + 3000) {
  const worker = new Worker(new URL(import.meta.url), { workerData: { kind: 'claude-note-model-identity', path } });
  return new Promise(resolveIdentity => {
    const finish = value => { clearTimeout(timer); void worker.terminate(); resolveIdentity(value); };
    const timer = setTimeout(() => finish({ revision: 'unknown', revision_kind: 'unknown' }), Math.max(1, deadline - Date.now()));
    worker.once('message', finish); worker.once('error', () => finish({ revision: 'unknown', revision_kind: 'unknown' }));
  });
}

export class ScopedLexicalWorker {
  constructor(options) { this.options = options; this.worker = null; this.serial = 0; }
  async request(message, deadline) {
    if (deadline <= Date.now()) throw err('deadline', 'Lexical deadline expired');
    if (!this.worker) this.worker = new Worker(new URL(import.meta.url), { workerData: { ...this.options, kind: 'claude-note-scoped-fts' } });
    const worker = this.worker, id = ++this.serial;
    return new Promise((accept, reject) => {
      const cleanup = () => { clearTimeout(timer); worker.off('message', onMessage); worker.off('error', onError); worker.off('exit', onExit); };
      const fail = e => { cleanup(); reject(e); };
      const onError = e => { if (this.worker === worker) this.worker = null; fail(e); };
      const onExit = () => { if (this.worker === worker) this.worker = null; fail(err('adapter_unavailable', 'Lexical worker exited')); };
      const onMessage = response => {
        if (response.id !== id && !response.error) return;
        cleanup();
        if (response.error) { this.close(); reject(err(response.error, 'Scoped lexical adapter unavailable')); }
        else accept(response);
      };
      const timer = setTimeout(() => { this.close(); fail(err('deadline', 'Lexical worker deadline expired')); }, Math.max(1, deadline - Date.now()));
      worker.on('message', onMessage); worker.once('error', onError); worker.once('exit', onExit);
      worker.postMessage({ ...message, id });
    });
  }
  close() { const worker = this.worker; this.worker = null; if (worker) void worker.terminate(); }
}

export function createRecallService({ store, llm, lexical, manifest = {}, queueLimit = 8, maxRequestMs = 8000,
  metadataTimeoutMs = 3000, modelAvailable = () => true, fallbackLexical, onFatal = () => {}, onJobStart = () => {}, now = Date.now }) {
  const started = now(), waiting = [];
  let active = false, poisoned = false, closed = false, expired = 0, rejected = 0;
  let metadataRefresh = null, activeJob = null;
  let metadata = {}, lexicalReady = false, vectorObserved = false, rerankObserved = false;
  let rerankStatus = 'not_observed';
  const degradation = new Set();
  const capabilities = () => ({ ...manifest, protocol_version: 2, engine: 'qmd-sdk', ok: !poisoned && !closed,
    index: { ...manifest.index, ...metadata,
      generation: metadata.generation ? digest(JSON.stringify([metadata.generation, manifest.index?.config_revision,
        manifest.models?.embed?.id, manifest.models?.embed?.revision, manifest.models?.rerank?.id, manifest.models?.rerank?.revision])) : undefined }, lexical_ready: lexicalReady && !poisoned && !closed,
    vector_ready: vectorObserved && !poisoned && !closed, rerank_ready: rerankObserved && !poisoned && !closed,
    rerank_status: rerankStatus,
    models: { ...manifest.models, embed: { ...manifest.models?.embed, ready: vectorObserved && !poisoned && !closed },
      rerank: { ...manifest.models?.rerank, ready: rerankObserved && !poisoned && !closed } },
    degradation: [...degradation], queue: { active, pending: waiting.length, limit: queueLimit, expired, rejected },
    uptime_s: Math.floor((now() - started) / 1000), loaded_ms: 0, asof_ms: now() });
  function remaining(deadline) {
    if (closed) throw err('closed', 'Server closed');
    if (now() >= deadline) throw err('deadline', 'Request deadline expired');
  }
  function beginMetadataRefresh(deadline) {
    const operation = { done: false, timer: null, deadline };
    operation.promise = new Promise(resolve => { operation.resolve = resolve; });
    operation.finish = (error, response, closeWorker = false) => {
      if (operation.done) return;
      operation.done = true; clearTimeout(operation.timer);
      if (closeWorker) {
        try { lexical.close?.(); } catch (failure) { error ||= failure; }
      }
      let nextMetadata;
      try {
        nextMetadata = response?.metadata;
        if (!error && (!nextMetadata || typeof nextMetadata !== 'object' || Array.isArray(nextMetadata)))
          error = err('index_metadata_unavailable', 'Invalid metadata response');
      } catch (failure) { error ||= failure; }
      if (!closed) {
        if (error) degradation.add(error.code || 'index_metadata_unavailable');
        else { metadata = nextMetadata; lexicalReady = true; }
      }
      if (metadataRefresh === operation) metadataRefresh = null;
      operation.resolve();
    };
    metadataRefresh = operation;
    // Assign shared ownership before invoking an adapter, including one that
    // throws synchronously. Its timer is installed first if it has its own.
    void Promise.resolve().then(() => {
      if (operation.done) return;
      remaining(deadline);
      const pending = lexical.request({ kind: 'metadata' }, deadline);
      operation.timer = setTimeout(() => operation.finish(err('deadline', 'Metadata deadline expired'), null, true),
        Math.max(1, deadline - now()));
      Promise.resolve(pending).then(response => {
        if (now() >= deadline) operation.finish(err('deadline', 'Metadata deadline expired'), null, true);
        else operation.finish(null, response);
      }, error => operation.finish(error));
    }).catch(error => operation.finish(error));
    return operation;
  }
  function waitForMetadata(operation, deadline) {
    if (deadline >= operation.deadline) return operation.promise.then(() => capabilities());
    // A short /health waiter may leave without cancelling the shared borrow.
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(capabilities()), Math.max(1, deadline - now()));
      void operation.promise.then(() => { clearTimeout(timer); resolve(capabilities()); });
    });
  }
  async function modelCall(fn, deadline) {
    remaining(deadline);
    if (poisoned) throw err('poisoned', 'Model worker requires restart');
    let timer;
    try {
      return await Promise.race([Promise.resolve().then(() => { remaining(deadline); return fn(); }), new Promise((_, reject) => {
        timer = setTimeout(() => {
          poisoned = true; degradation.add('model_deadline_requires_restart');
          lexical.close?.(); reject(err('deadline', 'Model deadline expired; restart required'));
          onFatal();
        }, Math.max(1, deadline - now()));
      })]);
    } finally { clearTimeout(timer); }
  }
  async function lexicalSearch(query, collections, limit, deadline) {
    try {
      remaining(deadline);
      const response = await lexical.request({ kind: 'search', query, collections, limit }, deadline);
      remaining(deadline);
      metadata = response.metadata; lexicalReady = true;
      return { rows: response.rows, coverage: 'complete_scoped_before_limit' };
    } catch (e) {
      if (e.code === 'deadline' || e.code === 'closed') throw e;
      degradation.add(e.code || 'scoped_adapter_unavailable');
      // Compatibility path is explicitly partial. Widening is bounded and can
      // never prove that the SDK's global prefilter found every scoped match.
      let rows = [];
      for (const width of [...new Set([limit, Math.min(limit * 4, 200), Math.min(limit * 16, 800)])]) {
        remaining(deadline);
        const found = fallbackLexical ? await fallbackLexical(query, collections, width, deadline)
          : await modelCall(() => store.searchLex(query, { limit: width, collection: collections.length ? collections : undefined }), deadline);
        lexicalReady = true;
        rows = found.filter(row => !collections.length || collections.includes(String(row.filepath || row.file).split('/')[2])).slice(0, limit);
        if (rows.length >= limit) break;
      }
      return { rows, coverage: 'partial_bounded_sdk_overfetch' };
    }
  }
  async function search(body, deadline) {
    const t = performance.now(), collections = collectionsOf(body);
    const lex = [], coverage = { lexical: 'complete_scoped_before_limit', semantic: 'disabled' };
    for (const rung of (Array.isArray(body.rungs) ? body.rungs : []).slice(0, 8)) {
      let got;
      try { got = await lexicalSearch(String(rung).slice(0, 2000), collections, boundedInt(body.lex_limit, 20, 100), deadline); }
      catch (e) { if (e.code !== 'deadline' || !lex.length) throw e; coverage.lexical = 'partial_deadline'; break; }
      lex.push(got.rows.map(row => pick(row, body.question || rung, body.passage_chars, true)));
      if (got.coverage.startsWith('partial')) coverage.lexical = got.coverage;
    }
    const tl = performance.now(); let vec = [];
    if (body.question) {
      if (modelAvailable('embed')) {
        try {
          const rows = await modelCall(() => store.searchVector(String(body.question).slice(0, 2400), {
            limit: boundedInt(body.vec_limit, 40, 100), collection: collections.length ? collections : undefined }), deadline);
          vec = rows.filter(row => !collections.length || collections.includes(String(row.filepath || row.file).split('/')[2]))
            .map(row => pick(row, body.question, body.passage_chars));
          vectorObserved = Boolean(llm?.embedContexts?.length || llm?.embedModel || rows.length)
            && Boolean(metadata.vector_rows) && (!manifest.models?.embed?.id || metadata.vector_models?.includes(manifest.models.embed.id));
          // QMD scopes exact vector scans through 20k rows, then uses capped ANN.
          const exactScope = collections.length && collections.every(name => {
            const count = metadata.collections?.find(c => c.name === name)?.vector_rows;
            return Number.isFinite(count) && count <= 20000;
          });
          coverage.semantic = collections.length ? exactScope ? 'complete_scoped_exact_vectors' : 'partial_qmd_scoped_vector_ann_limit' : 'qmd_vector_topk';
        } catch (e) {
          if ((poisoned || e.code === 'deadline') && !lex.length) throw e;
          coverage.semantic = e.code === 'deadline' ? 'partial_deadline' : 'unavailable'; degradation.add('vector_unavailable');
        }
      } else { coverage.semantic = 'model_cache_unavailable'; degradation.add('embed_model_cache_unavailable'); }
    }
    return { lex, vec, coverage, partial: Object.values(coverage).some(value => /partial|unavailable/.test(value)),
      manifest: capabilities(), ms: { lex: Math.round(tl - t), vec: Math.round(performance.now() - tl) } };
  }
  async function rerank(body, deadline) {
    // This is an observed SDK scoring contract, not evidence of native/GPU
    // execution. A previous success cannot make a later fallback look ready.
    rerankObserved = false; rerankStatus = 'pending';
    let t = performance.now();
    const finish = (status, coverage, model = 'unknown', scores = {}, passage_scores = {}) => {
      rerankStatus = status;
      rerankObserved = status === 'scored';
      return { scores, passage_scores, scored_passage_ids: Object.keys(passage_scores), coverage,
        rerank_status: status, model, manifest: capabilities(), ms: Math.round(performance.now() - t) };
    };
    if (!modelAvailable('rerank')) {
      degradation.add('rerank_model_cache_unavailable');
      return finish('model_cache_unavailable', 'model_cache_unavailable');
    }
    const docs = [];
    if (Array.isArray(body.passages)) {
      for (const p of body.passages.slice(0, 20)) {
        if (!p || typeof p.text !== 'string' || Buffer.byteLength(p.text) > 16000
            || typeof p.passage_id !== 'string' || !p.passage_id || Buffer.byteLength(p.passage_id) > 256
            || typeof p.revision !== 'string' || !p.revision || Buffer.byteLength(p.revision) > 128
            || docs.some(d => d.file === p.passage_id)) {
          rerankStatus = 'invalid_passage';
          throw err('invalid_passage', 'Unique bounded versioned passage required');
        }
        docs.push({ file: p.passage_id, title: String(p.title || '').slice(0, 160), text: p.text, original_file: String(p.file || '') });
      }
    } else {
      // Legacy clients get a selected passage, never an unrelated fixed prefix.
      for (const file of (body.files || []).slice(0, 20)) {
        const d = await modelCall(() => store.get(String(file), { includeBody: true }), deadline);
        if (!d) continue;
        const p = pick({ ...d, file: String(file) }, body.question, body.chars);
        if (p.scored_text) docs.push({ file: p.passage_id, title: p.title, text: p.scored_text, original_file: String(file) });
      }
    }
    if (!docs.length) return finish('not_requested', 'no_passages');
    const expectedModel = manifest.models?.rerank?.id;
    if (typeof expectedModel !== 'string' || !expectedModel || expectedModel.trim() !== expectedModel
        || Buffer.byteLength(expectedModel) > 1024 || /[\x00-\x1f\x7f]/.test(expectedModel)
        || /[\uD800-\uDFFF]/u.test(expectedModel)
        || ['fallback', 'unknown'].includes(expectedModel)) {
      degradation.add('rerank_unverified_model');
      return finish('unverified_model', 'unverified_model');
    }
    t = performance.now();
    let ranked;
    try { ranked = await modelCall(() => llm.rerank(String(body.question || '').slice(0, 2400), docs), deadline); }
    catch (error) { rerankStatus = error.code === 'deadline' ? 'deadline' : 'failed'; throw error; }
    // QMD 2.8.3 returns finite constant scores with model="fallback" when it
    // cannot create a ranking context. Reject the discriminator, not score .5.
    if (ranked?.model === 'fallback') {
      degradation.add('rerank_sdk_fallback');
      return finish('sdk_fallback', 'sdk_fallback_no_rerank', 'fallback');
    }
    if (ranked?.model !== expectedModel) {
      degradation.add('rerank_unverified_model');
      return finish('unverified_model', 'unverified_model');
    }
    const byId = new Map(docs.map(doc => [doc.file, doc])), accepted = new Map();
    let invalid = !Array.isArray(ranked.results) || ranked.results.length > docs.length;
    for (const row of invalid ? [] : ranked.results) {
      if (!row || !byId.has(row.file) || !Number.isFinite(row.score) || accepted.has(row.file)) {
        invalid = true; break;
      }
      accepted.set(row.file, row.score);
    }
    if (invalid) {
      degradation.add('rerank_invalid_scores');
      return finish('invalid_scores', 'invalid_scores', expectedModel);
    }
    if (!accepted.size) {
      degradation.add('rerank_no_scores_observed');
      return finish('no_valid_scores', 'no_valid_scores', expectedModel);
    }
    const scores = Object.fromEntries([...accepted].map(([id, score]) => [byId.get(id).original_file, score]));
    const passage_scores = Object.fromEntries(accepted);
    if (accepted.size !== docs.length) {
      degradation.add('rerank_partial_scores');
      return finish('partial_scores', 'partial_passage_scores', expectedModel, scores, passage_scores);
    }
    return finish('scored', 'exact_passage', expectedModel, scores, passage_scores);
  }
  async function drain() {
    if (active || closed) return;
    active = true;
    try {
      // Claim query ownership before waiting. New metadata reads use cached
      // capabilities until this query drain releases the lexical worker.
      if (metadataRefresh) await metadataRefresh.promise;
      while (waiting.length && !closed) {
        const job = waiting.shift();
        if (job.cancelled() || now() >= job.deadline) { expired++; job.reject(err('deadline', 'Queued request expired')); continue; }
        if (poisoned) { job.reject(err('poisoned', 'Model worker requires restart')); continue; }
        activeJob = job;
        try {
          onJobStart(job.body.request_id);
          job.resolve(await (job.route === '/search' ? search(job.body, job.deadline) : rerank(job.body, job.deadline)));
        } catch (e) { job.reject(e); }
        finally { activeJob = null; }
      }
    } finally { active = false; }
  }
  return {
    capabilities,
    refresh(deadline = now() + 300) {
      if (active || closed || poisoned) return Promise.resolve(capabilities());
      const requested = Number.isFinite(deadline) ? deadline : now();
      if (requested <= now()) return Promise.resolve(capabilities());
      // Health callers have short response budgets, but do not own the shared
      // SQLite borrow. Let one bounded refresh finish across later polls.
      const operation = metadataRefresh || beginMetadataRefresh(now() + boundedInt(metadataTimeoutMs, 3000, 3000));
      return waitForMetadata(operation, requested);
    },
    request(route, body, cancelled = () => false) {
      if (closed) return Promise.reject(err('closed', 'Server closed'));
      if (!['/search', '/rerank'].includes(route)) return Promise.reject(err('not_found', 'Unknown route'));
      if (body.index_id && body.index_id !== (manifest.index?.id || 'default')) return Promise.reject(err('index_mismatch', 'Index identity mismatch'));
      if (poisoned) return Promise.reject(err('poisoned', 'Model worker requires restart'));
      if (waiting.length >= queueLimit) { rejected++; return Promise.reject(err('queue_full', 'Recall queue full')); }
      const deadline = Math.min(now() + maxRequestMs, Number(body.deadline_at_ms) || now() + maxRequestMs);
      return new Promise((resolveJob, reject) => {
        let job;
        const timer = setTimeout(() => {
          const i = waiting.indexOf(job);
          if (i >= 0) { waiting.splice(i, 1); expired++; reject(err('deadline', 'Queued request expired')); }
        }, Math.max(1, deadline - now()));
        job = { route, body, deadline, cancelled, resolve: value => { clearTimeout(timer); resolveJob(value); },
          reject: e => { clearTimeout(timer); reject(e); }, timer };
        waiting.push(job);
        void drain();
      });
    },
    close() {
      if (closed) return;
      closed = true;
      try { lexical.close?.(); } catch (error) { degradation.add(error?.code || 'adapter_close_failed'); }
      metadataRefresh?.finish(err('closed', 'Server closed'));
      // Settle callers, but never claim to cancel an active native model. It
      // retains ownership until completion/deadline and no new work is admitted.
      activeJob?.reject(err('closed', 'Server closed'));
      for (const job of waiting.splice(0)) job.reject(err('closed', 'Server closed'));
    },
  };
}

export function createHttpServer(service) {
  return http.createServer(async (req, res) => {
    const send = (code, value) => { if (!res.destroyed) { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); } };
    if (!/^(?:127\.0\.0\.1|localhost)(?::\d+)?$/.test(req.headers.host || '')) return send(403, { error: 'loopback_host_required' });
    if (req.headers.origin && ![`http://127.0.0.1:${req.socket.localPort}`, `http://localhost:${req.socket.localPort}`].includes(req.headers.origin))
      return send(403, { error: 'loopback_origin_required' });
    if (req.method === 'GET' && req.url === '/health') return send(200, await service.refresh());
    if (req.method !== 'POST' || !['/search', '/rerank'].includes(req.url)) return send(404, { error: 'not found' });
    let raw = '', bytes = 0;
    req.on('data', chunk => { bytes += chunk.length; if (bytes > 1e6) req.destroy(); else raw += chunk; });
    req.on('end', async () => {
      try { send(200, await service.request(req.url, JSON.parse(raw || '{}'), () => res.destroyed)); }
      catch (e) { send(e.code === 'deadline' ? 504 : e.code === 'queue_full' || e.code === 'poisoned' ? 503 : 400,
        { error: e.code || 'request_failed', manifest: service.capabilities() }); }
    });
  });
}

function cachedModel(llm, kind) {
  const uri = llm[`${kind}ModelName`];
  let candidates = [];
  if (uri && !uri.startsWith('hf:')) candidates = [uri];
  else {
    const cache = llm.modelCacheDir;
    try { candidates = readdirSync(cache).filter(name => name.includes(basename(uri || '')) && name.endsWith('.gguf')).map(name => join(cache, name)); } catch { /* unavailable */ }
  }
  for (const path of candidates) {
    let fd;
    try {
      fd = openSync(path, 'r'); const header = Buffer.alloc(4); readSync(fd, header, 0, 4, 0);
      const stat = statSync(path);
      if (header.toString() !== 'GGUF' || !stat.isFile() || stat.size < 1024) continue;
      let revision = 'unknown';
      try { revision = readFileSync(join(llm.modelCacheDir, `${basename(uri)}.etag`), 'utf8').trim() || 'unknown'; } catch { /* explicitly unknown */ }
      return { id: uri, revision: 'unknown', revision_kind: 'unknown', upstream_revision: revision,
        cached: true, path, size_bytes: stat.size };
    } catch { /* invalid cache entry */ } finally { if (fd !== undefined) closeSync(fd); }
  }
  return { id: uri || 'unknown', revision: 'unknown', revision_kind: 'unknown', cached: false };
}

async function buildRuntime({ sdkPath, dbPath, configPath, indexId = 'default', onFatal = () => {}, onJobStart = () => {} }) {
  const requireSDK = createRequire(pathToFileURL(resolve(sdkPath)));
  const pkg = JSON.parse(readFileSync(join(dirname(sdkPath), '..', 'package.json'), 'utf8'));
  if (pkg.version !== PIN) throw new Error(`QMD ${PIN} required; found ${pkg.version}`);
  const sdk = await import(pathToFileURL(resolve(sdkPath)).href);
  const store = await sdk.createStore({ dbPath, configPath });
  const llm = store.internal.llm;
  const loadRuntime = llm.loadLlamaRuntime.bind(llm);
  llm.loadLlamaRuntime = () => loadRuntime(false); // pinned SDK: no native source download/build during recall
  const models = { embed: cachedModel(llm, 'embed'), rerank: cachedModel(llm, 'rerank') };
  await Promise.all(Object.values(models).filter(model => model.cached).map(async model => {
    Object.assign(model, await modelFileIdentity(model.path));
  }));
  // A pinned, inspected advanced adapter. No resolveModelFile()/network calls.
  llm.resolveModel = async uri => {
    const entry = Object.values(models).find(model => model.id === uri);
    if (!entry?.cached) throw err('model_cache_unavailable', 'Model must be provisioned before recall');
    return entry.path;
  };
  const lexical = new ScopedLexicalWorker({ dbPath: store.dbPath, sqliteModule: requireSDK.resolve('better-sqlite3'), version: pkg.version,
    sdkStorePath: join(dirname(sdkPath), 'store.js') });
  const service = createRecallService({ store, llm, lexical,
    modelAvailable: kind => models[kind]?.cached,
    fallbackLexical: async (query, collections, limit, deadline) => (await lexical.request({ kind: 'sdk_fallback', query, collections, limit }, deadline)).rows,
    manifest: { index: { id: indexId, db_path: store.dbPath, config_path: configPath,
      config_revision: digest(readFileSync(configPath)) },
      qmd: { version: pkg.version, native_runtime_policy: 'existing_prebuilt_only', sdk_revision: digest(Buffer.concat(['index.js', 'store.js', 'llm.js']
        .map(name => readFileSync(join(dirname(sdkPath), name))))), revision_kind: 'sdk_bundle_sha256' },
      models: Object.fromEntries(Object.entries(models).map(([kind, { path, ...model }]) => [kind, model])) },
    onFatal, onJobStart });
  await service.refresh(Date.now() + 3000);
  return service;
}

if (!isMainThread && workerData?.kind === 'claude-note-runtime') {
  buildRuntime({ ...workerData, onFatal: () => parentPort.postMessage({ fatal: true }),
    onJobStart: request_id => parentPort.postMessage({ started: request_id }) }).then(service => {
    const cancelled = new Set(), requests = new Set();
    parentPort.postMessage({ ready: true, manifest: service.capabilities() });
    parentPort.on('message', async message => {
      if (message.cancel_id) { if (requests.has(message.cancel_id)) cancelled.add(message.cancel_id); return; }
      requests.add(String(message.id));
      try {
        const result = message.route === '/health' ? await service.refresh(message.deadline)
          : await service.request(message.route, message.body, () => cancelled.has(String(message.id)));
        parentPort.postMessage({ id: message.id, result, manifest: service.capabilities() });
      } catch (e) { parentPort.postMessage({ id: message.id, error: e.code || 'request_failed', manifest: service.capabilities() }); }
      finally { cancelled.delete(String(message.id)); requests.delete(String(message.id)); }
    });
  }).catch(e => parentPort.postMessage({ startup_error: String(e.message || e).slice(0, 300) }));
}

// The parent event loop performs no QMD/SQLite/model calls. It can therefore
// enforce a deadline even when a native SDK call blocks the runtime's loop.
export function createRuntimeProxy(worker, { onFatal = () => {}, startupMs = 15000, queueLimit = 8 } = {}) {
  // This is the proxy lifetime, not a process-spawn or Worker-construction watchdog.
  const startupDeadline = performance.now() + startupMs;
  const pending = new Map(), expiredJobs = new Map();
  let serial = 0, poisoned = false, manifest = { ok: false }, refreshPromise, expired = 0, rejected = 0;
  let acceptReady, rejectReady, startupTimer, readySettled = false;
  const ready = new Promise((accept, reject) => { acceptReady = accept; rejectReady = reject; });
  // Close can precede a caller awaiting startup. The returned promise still rejects.
  void ready.catch(() => {});
  function settleReady(error) {
    if (readySettled) return;
    readySettled = true; clearTimeout(startupTimer);
    if (error) rejectReady(error); else acceptReady();
  }
  function poison(reason = err('poisoned', 'Runtime requires restart')) {
    settleReady(reason);
    if (poisoned) return;
    poisoned = true;
    void worker.terminate();
    for (const [id, job] of pending) { clearTimeout(job.timer); job.reject(err('poisoned', 'Runtime requires restart')); pending.delete(id); }
    expiredJobs.clear();
    onFatal();
  }
  startupTimer = setTimeout(() => poison(err('deadline', 'Runtime startup deadline')),
    Math.max(1, startupDeadline - performance.now()));
  worker.on('message', message => {
    if (poisoned) return;
    if (message.ready) {
      if (readySettled) return;
      // A delayed timer callback cannot let late readiness win the deadline race.
      if (performance.now() >= startupDeadline) { poison(err('deadline', 'Runtime startup deadline')); return; }
      if (message.manifest) manifest = message.manifest;
      settleReady(); return;
    }
    if (message.manifest) manifest = message.manifest;
    if (message.startup_error) { poison(err('startup', message.startup_error)); return; }
    if (message.fatal) { poison(); return; }
    if (message.started) {
      const id = Number(message.started), job = pending.get(id);
      if (expiredJobs.has(id)) poison();
      else if (job) job.started = true;
      return;
    }
    if (expiredJobs.has(message.id)) { expiredJobs.delete(message.id); return; }
    const job = pending.get(message.id);
    if (!job) return;
    clearTimeout(job.timer); pending.delete(message.id);
    if (message.error) job.reject(err(message.error, 'Runtime request failed'));
    else job.resolve(message.result);
  });
  worker.on('error', error => poison(error));
  worker.on('exit', () => poison(err('closed', 'Runtime exited')));
  function dispatch(route, body, deadline, fatalOnTimeout) {
    if (poisoned) return Promise.reject(err('poisoned', 'Runtime requires restart'));
    const id = ++serial;
    return new Promise((resolveJob, reject) => {
      const timer = setTimeout(() => {
        const job = pending.get(id);
        pending.delete(id); expired++; reject(err('deadline', 'Runtime absolute deadline expired'));
        if (fatalOnTimeout && job?.started) poison();
        else if (fatalOnTimeout) {
          // Keep a bounded tombstone until the runtime acknowledges/removes the
          // job. A late started message closes the cancellation/start race.
          expiredJobs.set(id, job);
          worker.postMessage({ cancel_id: String(id) });
        }
      }, Math.max(1, deadline - Date.now()));
      pending.set(id, { resolve: resolveJob, reject, timer, route, started: false });
      worker.postMessage({ id, route, body: { ...body, request_id: String(id) }, deadline });
    });
  }
  const capabilities = () => ({ ...manifest, ok: Boolean(manifest.ok) && !poisoned,
    lexical_ready: Boolean(manifest.lexical_ready) && !poisoned,
    vector_ready: Boolean(manifest.vector_ready) && !poisoned,
    rerank_ready: Boolean(manifest.rerank_ready) && !poisoned,
    models: { ...manifest.models, embed: { ...manifest.models?.embed, ready: Boolean(manifest.models?.embed?.ready) && !poisoned },
      rerank: { ...manifest.models?.rerank, ready: Boolean(manifest.models?.rerank?.ready) && !poisoned } },
    queue: { active: [...pending.values()].some(job => job.started),
      pending: [...pending.values()].filter(job => job.route !== '/health' && !job.started).length + expiredJobs.size,
      limit: queueLimit, expired, rejected },
    degradation: [...(manifest.degradation || []), ...(poisoned ? ['runtime_deadline_requires_restart'] : [])] });
  return {
    ready, capabilities,
    refresh(deadline = Date.now() + 300) {
      if (poisoned) return Promise.resolve(capabilities());
      refreshPromise ||= dispatch('/health', {}, deadline, false).then(() => capabilities()).catch(() => capabilities()).finally(() => { refreshPromise = null; });
      return refreshPromise;
    },
    request(route, body, cancelled = () => false) {
      if (cancelled()) return Promise.reject(err('deadline', 'Disconnected request'));
      if ([...pending.values()].filter(job => job.route !== '/health').length + expiredJobs.size >= queueLimit + 1) {
        rejected++;
        return Promise.reject(err('queue_full', 'Recall queue full'));
      }
      const deadline = Math.min(Date.now() + 8000, Number(body.deadline_at_ms) || Date.now() + 8000);
      if (deadline <= Date.now()) return Promise.reject(err('deadline', 'Request already expired'));
      return dispatch(route, { ...body, deadline_at_ms: deadline }, deadline, true);
    },
    close() {
      settleReady(err('closed', 'Runtime closed'));
      if (poisoned) return;
      poisoned = true; void worker.terminate();
      for (const job of pending.values()) { clearTimeout(job.timer); job.reject(err('closed', 'Runtime closed')); }
      pending.clear(); expiredJobs.clear();
    },
  };
}

async function main() {
  const [sdkPath, portArg, dbArg, ymlArg] = process.argv.slice(2);
  if (!sdkPath) throw new Error('usage: node recall_server.mjs <qmd-sdk-index.js> [port=8182] [index-db] [index-yml]');
  const dbPath = dbArg || join(process.env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'qmd', 'index.sqlite');
  const configPath = ymlArg || join(process.env.QMD_CONFIG_DIR || join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'qmd'), 'index.yml');
  const worker = new Worker(new URL(import.meta.url), { workerData: { kind: 'claude-note-runtime', sdkPath, dbPath,
    configPath, indexId: process.env.CLAUDE_NOTE_INDEX_ID || 'default' } });
  let fatalTimer;
  const service = createRuntimeProxy(worker, { onFatal: () => { fatalTimer ||= setTimeout(() => process.exit(75), 100); } });
  await service.ready;
  const server = createHttpServer(service);
  server.requestTimeout = 10000; server.headersTimeout = 10000;
  server.listen(Number(portArg || 8182), '127.0.0.1', () => console.error(`claude-note recall server on 127.0.0.1:${server.address().port} (QMD ${PIN}; models load from existing cache on demand)`));
  process.once('SIGTERM', () => { service.close(); server.close(); process.exit(0); });
}

if (isMainThread && process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(e => { console.error(String(e.message || e)); process.exitCode = 2; });
}
