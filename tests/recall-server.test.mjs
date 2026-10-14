import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { Worker } from 'node:worker_threads';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRecallService, createRuntimeProxy, ScopedLexicalWorker, passage, literalFTS, modelFileIdentity } from '../src/claude_note/recall_server.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const metadata = { corpus_digest: 'corpus', generation: 'generation', vector_rows: 1, vector_models: ['embed-id'] };
const row = { filepath: 'qmd://public/a.md', title: 'A', body: 'The retry handler must preserve idempotency keys across failed payment attempts.', score: .5 };
const lex = () => ({ request: async () => ({ rows: [row], metadata }), close() {} });

test('passage selection retains older matching evidence instead of an unrelated latest block', () => {
  const body = '# Earlier\n\nThe retry handler must preserve idempotency keys across failed payment attempts.\n\n# Latest\n\nBasil grows well beside a sunny window and needs occasional water during winter.\n';
  const p = passage(body, 'retry idempotency payment', 140);
  assert.match(p.text, /idempotency/);
  assert.doesNotMatch(p.text, /Basil/);
  assert.equal(p.anchor.start_line, 3);
  assert.equal(literalFTS('ключ Error_42 " OR x'), '"ключ"* AND "Error_42"* AND "OR"* AND "x"*');
});

test('scope is sent structurally to both lanes; rank sees and identifies exactly delivered passage', async () => {
  const seen = [];
  const service = createRecallService({ lexical: { request: async request => { seen.push(request); return { rows: [row], metadata }; } },
    llm: { embedModel: {}, rerank: async (_, docs) => { seen.push(docs); return { results: docs.map(d => ({ file: d.file, score: .8 })) }; } },
    store: { searchVector: async (_, options) => { seen.push(options); return [row, { ...row, filepath: 'qmd://private/secret.md' }]; } },
    manifest: { index: { id: 'default' }, models: { embed: { id: 'embed-id' } } } });
  const result = await service.request('/search', { rungs: ['retry'], question: 'retry idempotency', collections: ['public'] });
  assert.deepEqual(seen[0].collections, ['public']);
  assert.deepEqual(seen[1].collection, ['public']);
  assert.equal(result.vec.length, 1);
  assert.equal(result.coverage.lexical, 'complete_scoped_before_limit');
  assert.equal(result.manifest.vector_ready, true);
  const p = result.lex[0][0];
  const ranked = await service.request('/rerank', { passages: [{ ...p, text: p.scored_text }], question: 'retry' });
  assert.equal(seen[2][0].text, p.scored_text);
  assert.equal(seen[2][0].file, p.passage_id);
  assert.deepEqual(ranked.scored_passage_ids, [p.passage_id]);
  assert.equal(ranked.passage_scores[p.passage_id], .8);
  service.close();
});

test('lexical evidence remains available without cached models or vectors', async () => {
  let models = 0;
  const service = createRecallService({ lexical: lex(), modelAvailable: () => false,
    store: { searchVector: async () => { models++; throw new Error('unexpected'); } }, llm: {} });
  const result = await service.request('/search', { rungs: ['idempotency'], question: 'idempotency' });
  assert.equal(result.lex[0].length, 1);
  assert.equal(result.lex[0][0].lexical_evidence, true);
  assert.equal(models, 0);
  assert.equal(result.coverage.semantic, 'model_cache_unavailable');
  assert.equal(result.manifest.vector_ready, false);
  service.close();
});

test('SDK fallback widens finitely and remains observably partial', async () => {
  const widths = [];
  const service = createRecallService({ lexical: { request: async () => { throw Object.assign(new Error(), { code: 'unsupported_schema' }); } },
    modelAvailable: () => false, llm: {}, store: { searchLex: async (_, opts) => { widths.push(opts.limit); assert.deepEqual(opts.collection, ['public']); return []; } } });
  const result = await service.request('/search', { rungs: ['retry'], collections: ['public'], lex_limit: 3 });
  assert.deepEqual(widths, [3, 12, 48]);
  assert.equal(result.coverage.lexical, 'partial_bounded_sdk_overfetch');
  assert.equal(result.partial, true);
  service.close();
});

test('finite queue expires pending work and a model deadline never admits concurrent models', async () => {
  let modelCalls = 0, fatal = 0;
  const service = createRecallService({ lexical: lex(), queueLimit: 1, maxRequestMs: 200,
    store: { searchVector: async () => { modelCalls++; await new Promise(() => {}); } }, llm: {}, onFatal: () => fatal++ });
  const first = service.request('/search', { rungs: ['retry'], question: 'retry', deadline_at_ms: Date.now() + 100 });
  await sleep(5);
  const queued = service.request('/search', { question: 'queued', deadline_at_ms: Date.now() + 25 });
  const expired = assert.rejects(queued, e => e.code === 'deadline');
  await assert.rejects(service.request('/search', { question: 'rejected' }), e => e.code === 'queue_full');
  await expired;
  assert.equal(service.capabilities().queue.pending, 0);
  const result = await first;
  assert.equal(result.coverage.semantic, 'partial_deadline');
  assert.equal(result.lex[0].length, 1);
  await assert.rejects(service.request('/search', { question: 'never overlap' }), e => e.code === 'poisoned');
  assert.equal(modelCalls, 1);
  assert.equal(fatal, 1);
  assert.equal(service.capabilities().ok, false);
  service.close();
});

test('cancelled queued request is never searched and index requests cannot select an arbitrary DB', async () => {
  let release;
  const seen = [];
  const service = createRecallService({ lexical: lex(), store: { searchVector: async question => {
    seen.push(question); if (question === 'first') await new Promise(resolve => { release = resolve; }); return [];
  } }, llm: {}, manifest: { index: { id: 'known' } } });
  const first = service.request('/search', { question: 'first' });
  await sleep(5);
  const cancelled = assert.rejects(service.request('/search', { question: 'cancelled' }, () => true), e => e.code === 'deadline');
  release(); await first; await cancelled;
  assert.deepEqual(seen, ['first']);
  await assert.rejects(service.request('/search', { index_id: '/tmp/another.sqlite' }), e => e.code === 'index_mismatch');
  service.close();
});

test('QMD2.8.3 scoped worker retrieves past more than ten times global distractors, read-only', { skip: !process.env.CLAUDE_NOTE_TEST_SQLITE }, async () => {
  const Database = createRequire(import.meta.url)(process.env.CLAUDE_NOTE_TEST_SQLITE);
  const dir = mkdtempSync(join(tmpdir(), 'claude-note-scoped-'));
  const dbPath = join(dir, 'fixture.sqlite');
  const db = new Database(dbPath);
  db.exec(`CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER);
    CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT);
    CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body);`);
  const doc = db.prepare('INSERT INTO documents VALUES(?,?,?,?,?,1)'), content = db.prepare('INSERT INTO content VALUES(?,?)');
  const fts = db.prepare('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)');
  for (let i = 1; i <= 502; i++) {
    const collection = i === 502 ? 'public' : 'distractors';
    const body = i === 502 ? `retry ${'padding '.repeat(250)}` : 'retry retry retry';
    doc.run(i, collection, `${i}.md`, 'Same title', `${i}`); content.run(`${i}`, body); fts.run(i, `${collection}/${i}.md`, 'Same title', body);
  }
  doc.run(503, 'public', 'cjk.md', 'Public CJK fixture', '503');
  content.run('503', '你好朋友：这是公开的搜索测试。');
  fts.run(503, 'public/cjk.md', 'Public CJK fixture', '你 好 朋 友 ： 这 是 公 开 的 搜 索 测 试 。');
  const globalTop = db.prepare("SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'retry' ORDER BY bm25(documents_fts) LIMIT 30").all();
  assert.equal(globalTop.some(row => row.rowid === 502), false);
  db.close();
  const worker = new ScopedLexicalWorker({ dbPath, sqliteModule: process.env.CLAUDE_NOTE_TEST_SQLITE, version: '2.8.3' });
  try {
    const result = await worker.request({ kind: 'search', query: 'retry', collections: ['public'], limit: 3 }, Date.now() + 3000);
    assert.equal(result.rows.length, 1);
    assert.equal(result.rows[0].filepath, 'qmd://public/502.md');
    assert.equal(result.metadata.collections.find(c => c.name === 'distractors').documents, 501);
    const cjk = await worker.request({ kind: 'search', query: '你好朋友', collections: ['public'], limit: 3 }, Date.now() + 3000);
    assert.equal(cjk.rows[0].filepath, 'qmd://public/cjk.md');
    await assert.rejects(worker.request({ kind: 'search', query: 'retry', collections: [], limit: 3 }, Date.now() - 1), e => e.code === 'deadline');
  } finally { worker.close(); rmSync(dir, { recursive: true, force: true }); }
});

test('model identity hashes actual cache bytes in a deadline-bounded worker', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'claude-note-model-identity-'));
  try {
    const path = join(dir, 'synthetic.gguf'), bytes = Buffer.from('GGUF public synthetic fixture bytes');
    writeFileSync(path, bytes);
    assert.deepEqual(await modelFileIdentity(path), { revision: createHash('sha256').update(bytes).digest('hex'), revision_kind: 'model_file_sha256' });
    assert.deepEqual(await modelFileIdentity(join(dir, 'missing.gguf')), { revision: 'unknown', revision_kind: 'unknown' });
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('production proxy deadline stays responsive when native runtime blocks its own event loop', async () => {
  const worker = new Worker(`const {parentPort}=require('node:worker_threads');
    parentPort.postMessage({ready:true,manifest:{ok:true,lexical_ready:true}});
    parentPort.on('message',msg=>{parentPort.postMessage({started:msg.body.request_id}); while(true){} });`, { eval: true });
  let fatal = 0;
  const proxy = createRuntimeProxy(worker, { onFatal: () => fatal++ });
  await proxy.ready;
  const began = Date.now();
  await assert.rejects(proxy.request('/search', { question: 'blocked public fixture', deadline_at_ms: Date.now() + 50 }), e => e.code === 'deadline');
  assert.ok(Date.now() - began < 250);
  assert.equal(fatal, 1);
  assert.equal(proxy.capabilities().ok, false);
  await assert.rejects(proxy.request('/search', {}), e => e.code === 'poisoned');
  proxy.close();
});

test('late start after queued expiry poisons runtime; acknowledged never-started cancellation does not', async () => {
  class FixtureWorker extends EventEmitter {
    sent = [];
    postMessage(message) { this.sent.push(message); }
    terminate() { return Promise.resolve(); }
  }
  const late = new FixtureWorker(); let fatal = 0;
  const proxy = createRuntimeProxy(late, { onFatal: () => fatal++ });
  late.emit('message', { ready: true, manifest: { ok: true } }); await proxy.ready;
  await assert.rejects(proxy.request('/search', { deadline_at_ms: Date.now() + 10 }), e => e.code === 'deadline');
  const expiredID = late.sent[0].id;
  assert.equal(proxy.capabilities().queue.pending, 1);
  late.emit('message', { started: String(expiredID) });
  assert.equal(fatal, 1); assert.equal(proxy.capabilities().ok, false); proxy.close();
  const cancelled = new FixtureWorker(); let cleanFatal = 0;
  const clean = createRuntimeProxy(cancelled, { onFatal: () => cleanFatal++ });
  cancelled.emit('message', { ready: true, manifest: { ok: true } }); await clean.ready;
  await assert.rejects(clean.request('/search', { deadline_at_ms: Date.now() + 10 }), e => e.code === 'deadline');
  cancelled.emit('message', { id: cancelled.sent[0].id, error: 'deadline' });
  assert.equal(clean.capabilities().queue.pending, 0); assert.equal(cleanFatal, 0); assert.equal(clean.capabilities().ok, true);
  clean.close();
});

test('resolved reranker with no finite scores does not claim readiness', async () => {
  const service = createRecallService({ store: {}, lexical: lex(), llm: { rerank: async () => ({ results: [] }) } });
  const result = await service.request('/rerank', { passages: [{ file: row.filepath, passage_id: 'p1', revision: 'r1', text: row.body }] });
  assert.equal(result.manifest.rerank_ready, false);
  assert.deepEqual(result.scored_passage_ids, []);
  assert.ok(result.manifest.degradation.includes('rerank_no_scores_observed'));
  service.close();
});

test('real pinned SDK and production HTTP worker serve a wholly isolated public lexical fixture',
  { skip: !process.env.CLAUDE_NOTE_TEST_QMD_SDK, timeout: 12000 }, async () => {
    const dir = mkdtempSync(join(tmpdir(), 'claude-note-http-fixture-'));
    const sdk = process.env.CLAUDE_NOTE_TEST_QMD_SDK, dbPath = join(dir, 'index.sqlite'), configPath = join(dir, 'index.yml');
    const body = 'Public synthetic canary: payment retries must reuse the same idempotency key to prevent duplicate charges.';
    const env = { ...process.env, XDG_CONFIG_HOME: join(dir, 'config'), QMD_CONFIG_DIR: dir,
      XDG_CACHE_HOME: join(dir, 'cache'), QMD_EMBED_MODEL: join(dir, 'absent-embed.gguf'), QMD_RERANK_MODEL: join(dir, 'absent-rerank.gguf') };
    writeFileSync(join(dir, 'canary.md'), body);
    writeFileSync(configPath, `collections:\n  public:\n    path: ${JSON.stringify(dir)}\n    pattern: "*.md"\n`);
    const seed = `import {pathToFileURL} from 'node:url'; import {createHash} from 'node:crypto';
      const sdk=await import(pathToFileURL(process.argv[1])); const store=await sdk.createStore({dbPath:process.argv[2],configPath:process.argv[3]});
      const db=store.internal.db,body=process.argv[4],hash=createHash('sha256').update(body).digest('hex');
      db.prepare('INSERT INTO content(hash,doc,created_at) VALUES(?,?,?)').run(hash,body,'2026-10-13');
      db.prepare('INSERT INTO documents(collection,path,title,hash,created_at,modified_at,active) VALUES(?,?,?,?,?,?,1)').run('public','canary.md','Public canary',hash,'2026-10-13','2026-10-13');
      await store.close();`;
    let server;
    try {
      const init = spawn(process.execPath, ['--input-type=module', '-e', seed, sdk, dbPath, configPath, body], { env, stdio: ['ignore', 'pipe', 'pipe'] });
      let initErrors = ''; init.stderr.on('data', chunk => { initErrors += chunk; });
      const [code] = await once(init, 'exit'); assert.equal(code, 0, initErrors);
      server = spawn(process.execPath, [fileURLToPath(new URL('../src/claude_note/recall_server.mjs', import.meta.url)), sdk, '0', dbPath, configPath],
        { env, stdio: ['ignore', 'pipe', 'pipe'] });
      const port = await new Promise((resolvePort, reject) => {
        let errors = '';
        const timer = setTimeout(() => reject(new Error(`Fixture startup timeout: ${errors}`)), 8000);
        server.stderr.on('data', chunk => { errors += chunk; const match = errors.match(/server on 127\.0\.0\.1:(\d+)/);
          if (match) { clearTimeout(timer); resolvePort(Number(match[1])); } });
        server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture server exited ${code}: ${errors}`)); });
      });
      const base = `http://127.0.0.1:${port}`;
      const health = await (await fetch(`${base}/health`)).json();
      assert.equal(health.qmd.version, '2.8.3'); assert.equal(health.lexical_ready, true); assert.equal(health.vector_ready, false);
      assert.equal(health.index.db_path, dbPath); assert.equal(health.index.collections[0].name, 'public');
      const foreignHostStatus = await new Promise((accept, reject) => {
        http.get(`${base}/health`, { headers: { host: 'untrusted.example' } }, response => {
          response.resume(); accept(response.statusCode);
        }).on('error', reject);
      });
      assert.equal(foreignHostStatus, 403);
      assert.equal((await fetch(`${base}/health`, { headers: { origin: 'https://untrusted.example' } })).status, 403);
      const response = await fetch(`${base}/search`, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ collections: ['public'], index_id: 'default', rungs: ['payment retries'], question: 'payment retries', deadline_at_ms: Date.now() + 1000 }) });
      assert.equal(response.status, 200);
      const result = await response.json();
      assert.equal(result.lex[0][0].file, 'qmd://public/canary.md');
      assert.equal(result.lex[0][0].revision, createHash('sha256').update(body).digest('hex'));
      assert.equal(result.coverage.lexical, 'complete_scoped_before_limit');
      assert.equal(result.coverage.semantic, 'model_cache_unavailable');
    } finally {
      if (server && server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
      rmSync(dir, { recursive: true, force: true });
    }
  });
