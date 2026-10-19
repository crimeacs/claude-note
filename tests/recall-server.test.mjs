import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { Worker } from 'node:worker_threads';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRecallService, createRuntimeProxy, ScopedLexicalWorker, createIndexSnapshotReader, scopedLexical, passage, literalFTS, modelFileIdentity } from '../src/claude_note/recall_server.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const metadata = { corpus_digest: 'corpus', generation: 'generation', vector_rows: 1, vector_models: ['embed-id'] };
const row = { filepath: 'qmd://public/a.md', title: 'A', body: 'The retry handler must preserve idempotency keys across failed payment attempts.', score: .5 };
const lex = () => ({ request: async () => ({ rows: [row], metadata }), close() {} });
const rerankModel = 'synthetic:public-reranker';
const rerankManifest = { models: { rerank: { id: rerankModel } } };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

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
    llm: { embedModel: {}, rerank: async (_, docs) => { seen.push(docs); return { model: rerankModel, results: docs.map(d => ({ file: d.file, score: .8 })) }; } },
    store: { searchVector: async (_, options) => { seen.push(options); return [row, { ...row, filepath: 'qmd://private/secret.md' }]; } },
    manifest: { index: { id: 'default' }, models: { embed: { id: 'embed-id' }, rerank: { id: rerankModel } } } });
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

test('metadata cache: real SQLite snapshots reuse scans and invalidate external document/vector commits', { skip: !process.env.CLAUDE_NOTE_TEST_SQLITE }, () => {
  const Database = createRequire(import.meta.url)(process.env.CLAUDE_NOTE_TEST_SQLITE);
  const dir = mkdtempSync(join(tmpdir(), 'claude-note-metadata-cache-')), dbPath = join(dir, 'fixture.sqlite');
  const writer = new Database(dbPath);
  writer.pragma('journal_mode=WAL');
  writer.exec(`CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER);
    CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT);
    CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body);
    CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT);`);
  const insert = id => {
    writer.prepare('INSERT INTO documents VALUES(?,?,?,?,?,1)').run(id, 'public', `${id}.md`, 'Public fixture', `${id}`);
    writer.prepare('INSERT INTO content VALUES(?,?)').run(`${id}`, `retry public fixture ${id}`);
    writer.prepare('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)').run(id, `public/${id}.md`, 'Public fixture', `retry public fixture ${id}`);
  };
  insert(1);
  const db = new Database(dbPath, { readonly: true });
  db.pragma('query_only=ON');
  let scans = 0, afterVersion;
  const traced = {
    exec: sql => db.exec(sql),
    pragma(sql, options) {
      const result = db.pragma(sql, options);
      if (sql === 'data_version' && afterVersion) { const effect = afterVersion; afterVersion = null; effect(); }
      return result;
    },
    prepare(sql) { if (sql.startsWith('SELECT collection,path,hash')) scans++; return db.prepare(sql); },
  };
  const read = createIndexSnapshotReader(traced);
  const search = () => read(() => scopedLexical(db, 'retry', ['public'], 10));
  try {
    const first = search(), cached = search();
    assert.equal(scans, 1); assert.strictEqual(cached.metadata, first.metadata);
    assert.equal(first.rows.length, 1); assert.equal(first.metadata.vector_rows, 0);
    writer.exec("INSERT INTO content_vectors VALUES('1',0,'embed-a')");
    const vectorOnly = search();
    assert.equal(scans, 2); assert.equal(vectorOnly.metadata.corpus_digest, first.metadata.corpus_digest);
    assert.equal(vectorOnly.metadata.vector_rows, 1); assert.deepEqual(vectorOnly.metadata.vector_models, ['embed-a']);
    writer.exec("UPDATE content_vectors SET model='embed-b'");
    assert.deepEqual(search().metadata.vector_models, ['embed-b']); assert.equal(scans, 3);
    writer.exec("BEGIN; INSERT INTO content_vectors VALUES('1',1,'rolled-back'); ROLLBACK;");
    assert.deepEqual(search().metadata.vector_models, ['embed-b']); assert.equal(scans, 3);
    // Commit after the cache-key read. Both rows and cached metadata must stay
    // on its established snapshot; the next transaction sees the new revision.
    afterVersion = () => writer.transaction(() => insert(2))();
    const duringCommit = search();
    assert.equal(duringCommit.rows.length, 1); assert.equal(duringCommit.metadata.collections[0].documents, 1);
    assert.equal(scans, 3);
    const changed = search();
    assert.equal(changed.rows.length, 2); assert.equal(changed.metadata.collections[0].documents, 2);
    assert.equal(scans, 4); assert.notEqual(changed.metadata.corpus_digest, first.metadata.corpus_digest);
    assert.equal(db.inTransaction, false);
  } finally { db.close(); writer.close(); rmSync(dir, { recursive: true, force: true }); }
});

test('metadata cache: real SQLite rolls back failed scans/rows and publishes only committed metadata', { skip: !process.env.CLAUDE_NOTE_TEST_SQLITE }, () => {
  const Database = createRequire(import.meta.url)(process.env.CLAUDE_NOTE_TEST_SQLITE);
  const dir = mkdtempSync(join(tmpdir(), 'claude-note-metadata-failure-')), dbPath = join(dir, 'fixture.sqlite');
  const writer = new Database(dbPath);
  writer.pragma('journal_mode=WAL');
  writer.exec(`CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER);
    CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT);
    INSERT INTO documents VALUES(1,'public','a.md','Public','hash-a',1);`);
  const db = new Database(dbPath, { readonly: true });
  db.pragma('query_only=ON');
  let scans = 0, failMetadata = false, failCommit = false;
  const read = createIndexSnapshotReader({ exec(sql) {
    if (sql === 'COMMIT' && failCommit) { failCommit = false; throw new Error('Synthetic commit failure'); }
    return db.exec(sql);
  }, pragma: (sql, options) => db.pragma(sql, options),
    prepare(sql) {
      if (sql.startsWith('SELECT collection,path,hash')) scans++;
      if (failMetadata && sql.includes('GROUP BY model')) { failMetadata = false; throw new Error('Synthetic metadata read failure'); }
      return db.prepare(sql);
    } });
  try {
    assert.equal(read().metadata.vector_rows, 0); assert.equal(scans, 1);
    writer.exec("INSERT INTO content_vectors VALUES('hash-a',0,'embed-a')");
    failMetadata = true;
    assert.throws(() => read(), /Synthetic metadata read failure/); assert.equal(db.inTransaction, false);
    assert.equal(read().metadata.vector_rows, 1); assert.equal(scans, 3);
    writer.exec("INSERT INTO content_vectors VALUES('hash-a',1,'embed-a')");
    assert.throws(() => read(() => { throw new Error('Synthetic rows read failure'); }), /Synthetic rows read failure/);
    assert.equal(db.inTransaction, false); assert.equal(scans, 4);
    assert.equal(read().metadata.vector_rows, 2); assert.equal(scans, 5);
    assert.equal(read().metadata.vector_rows, 2); assert.equal(scans, 5);
    writer.exec("INSERT INTO content_vectors VALUES('hash-a',2,'embed-a')");
    failCommit = true;
    assert.throws(() => read(), /Synthetic commit failure/); assert.equal(db.inTransaction, false);
    assert.equal(read().metadata.vector_rows, 3); assert.equal(scans, 7);
  } finally { db.close(); writer.close(); rmSync(dir, { recursive: true, force: true }); }
});

test('metadata cache: actual lexical worker refreshes after an external vector-only commit', { skip: !process.env.CLAUDE_NOTE_TEST_SQLITE }, async () => {
  const Database = createRequire(import.meta.url)(process.env.CLAUDE_NOTE_TEST_SQLITE);
  const dir = mkdtempSync(join(tmpdir(), 'claude-note-worker-cache-')), dbPath = join(dir, 'fixture.sqlite');
  const writer = new Database(dbPath);
  writer.pragma('journal_mode=WAL');
  writer.exec(`CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER);
    CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT);
    CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body);
    CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT);
    INSERT INTO documents VALUES(1,'public','a.md','Public','hash-a',1);
    INSERT INTO content VALUES('hash-a','retry public fixture');
    INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(1,'public/a.md','Public','retry public fixture');`);
  const worker = new ScopedLexicalWorker({ dbPath, sqliteModule: process.env.CLAUDE_NOTE_TEST_SQLITE, version: '2.8.3' });
  try {
    const first = await worker.request({ kind: 'metadata' }, Date.now() + 3000);
    assert.equal(first.metadata.vector_rows, 0);
    writer.exec("INSERT INTO content_vectors VALUES('hash-a',0,'embed-a')");
    const changed = await worker.request({ kind: 'search', query: 'retry', collections: ['public'], limit: 1 }, Date.now() + 3000);
    assert.equal(changed.rows[0].filepath, 'qmd://public/a.md');
    assert.equal(changed.metadata.vector_rows, 1); assert.deepEqual(changed.metadata.vector_models, ['embed-a']);
    assert.equal(changed.metadata.collections[0].vector_rows, 1);
    assert.notEqual(changed.metadata.generation, first.metadata.generation);
  } finally { worker.close(); writer.close(); rmSync(dir, { recursive: true, force: true }); }
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

test('runtime proxy startup has one monotonic readiness lifetime', async t => {
  const flush = async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); };
  const observe = promise => {
    const value = { state: 'pending', settlements: 0 };
    promise.then(() => Object.assign(value, { state: 'fulfilled', settlements: value.settlements + 1 }),
      error => Object.assign(value, { state: 'rejected', code: error.code, settlements: value.settlements + 1 }));
    return value;
  };
  const initial = { ok: true, lexical_ready: true, index: { generation: 'initial' } };
  const late = { ...initial, index: { generation: 'late' } };
  async function fixture(child, run) {
    let mono = 100, wall = 1700000000000, sequence = 0;
    const timers = new Map(), proxies = [];
    child.mock.method(performance, 'now', () => mono);
    child.mock.method(Date, 'now', () => wall);
    child.mock.method(globalThis, 'setTimeout', (fn, delay) => {
      const id = ++sequence; timers.set(id, { due: mono + Number(delay), fn }); return id;
    });
    child.mock.method(globalThis, 'clearTimeout', id => timers.delete(id));
    const advance = async (ms, callbacks = true) => {
      const target = mono + ms;
      if (callbacks) for (;;) {
        const item = [...timers].filter(([, timer]) => timer.due <= target)
          .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
        if (!item) break;
        wall += item[1].due - mono; mono = item[1].due;
        timers.delete(item[0]); item[1].fn(); await flush();
      }
      wall += target - mono; mono = target; await flush();
    };
    const make = () => {
      class FakeWorker extends EventEmitter {
        sent = []; terminations = 0;
        postMessage(message) { this.sent.push(message); }
        // Deliberately never acknowledges termination. Settlement cannot depend on exit.
        terminate() { this.terminations++; return new Promise(() => {}); }
      }
      const worker = new FakeWorker(); let fatal = 0;
      const proxy = createRuntimeProxy(worker, { startupMs: 20, onFatal: () => fatal++ });
      proxies.push(proxy);
      return { worker, proxy, ready: observe(proxy.ready), fatal: () => fatal };
    };
    try { await run({ make, advance, wallNow: () => wall, changeWall: delta => { wall += delta; } }); }
    finally {
      for (const proxy of proxies) proxy.close();
      await flush(); assert.equal(timers.size, 0, 'no startup or request timer survives close');
      child.mock.restoreAll();
    }
  }
  const run = (name, fn) => t.test(name, child => fixture(child, fn));
  await run('normal ready settles once without model readiness', async ({ make, advance }) => {
    const { worker, proxy, ready, fatal } = make(); await advance(5);
    worker.emit('message', { ready: true, manifest: initial }); await flush();
    worker.emit('message', { ready: true, manifest: late }); await advance(100);
    assert.deepEqual(ready, { state: 'fulfilled', settlements: 1 });
    assert.equal(proxy.capabilities().index.generation, 'initial'); assert.equal(fatal(), 0);
    assert.equal(proxy.capabilities().vector_ready, false); assert.equal(proxy.capabilities().rerank_ready, false);
  });
  for (const [event, code] of [['startup_error', 'startup'], ['error', 'fixture_error'], ['exit', 'closed']]) {
    await run(`${event} rejects startup once`, async ({ make, advance }) => {
      const { worker, proxy, ready, fatal } = make();
      if (event === 'startup_error') worker.emit('message', { startup_error: 'synthetic startup failure' });
      else if (event === 'error') worker.emit('error', Object.assign(new Error('fixture'), { code }));
      else worker.emit('exit', 1);
      await advance(100); proxy.close(); worker.emit('exit', 1); await flush();
      assert.deepEqual(ready, { state: 'rejected', code, settlements: 1 });
      assert.equal(worker.terminations, 1); assert.equal(fatal(), 1);
    });
  }
  await run('startup deadline rejects once', async ({ make, advance }) => {
    const { worker, ready, fatal } = make(); await advance(20);
    assert.deepEqual(ready, { state: 'rejected', code: 'deadline', settlements: 1 });
    assert.equal(worker.terminations, 1); assert.equal(fatal(), 1);
  });
  await run('close settles startup without termination acknowledgement', async ({ make, advance }) => {
    const { worker, proxy, ready, fatal } = make(); proxy.close(); proxy.close(); await advance(100);
    assert.deepEqual(ready, { state: 'rejected', code: 'closed', settlements: 1 });
    assert.equal(worker.terminations, 1); assert.equal(fatal(), 0);
  });
  await run('close ignores late ready before manifest mutation', async ({ make }) => {
    const { worker, proxy, ready } = make(); proxy.close();
    worker.emit('message', { ready: true, manifest: late }); await flush();
    assert.equal(ready.code, 'closed'); assert.equal(ready.settlements, 1);
    assert.equal(proxy.capabilities().index, undefined); assert.equal(proxy.capabilities().ok, false);
  });
  await run('fatal settles unresolved startup immediately', async ({ make }) => {
    const { worker, ready, fatal } = make(); worker.emit('message', { fatal: true }); await flush();
    assert.deepEqual(ready, { state: 'rejected', code: 'poisoned', settlements: 1 });
    assert.equal(worker.terminations, 1); assert.equal(fatal(), 1);
  });
  await run('fatal ignores late ready and identity', async ({ make }) => {
    const { worker, proxy, ready } = make(); worker.emit('message', { fatal: true });
    worker.emit('message', { ready: true, manifest: late }); await flush();
    assert.equal(ready.code, 'poisoned'); assert.equal(proxy.capabilities().index, undefined);
  });
  await run('expired startup ignores late ready and identity', async ({ make, advance }) => {
    const { worker, proxy, ready } = make(); await advance(20);
    worker.emit('message', { ready: true, manifest: late }); await flush();
    assert.equal(ready.code, 'deadline'); assert.equal(proxy.capabilities().index, undefined);
  });
  await run('close rejects pending job and ignores late result identity', async ({ make, wallNow }) => {
    const { worker, proxy } = make(); worker.emit('message', { ready: true, manifest: initial }); await flush();
    const job = observe(proxy.request('/search', { deadline_at_ms: wallNow() + 10 }));
    proxy.close(); worker.emit('message', { id: worker.sent[0].id, result: {}, manifest: late }); await flush();
    assert.deepEqual(job, { state: 'rejected', code: 'closed', settlements: 1 });
    assert.equal(proxy.capabilities().index.generation, 'initial');
  });
  await run('started request deadline poisons without replacing owner', async ({ make, advance, wallNow }) => {
    const { worker, proxy, fatal } = make(); worker.emit('message', { ready: true, manifest: initial }); await flush();
    const job = observe(proxy.request('/search', { deadline_at_ms: wallNow() + 10 }));
    worker.emit('message', { started: String(worker.sent[0].id) }); await advance(10);
    assert.equal(job.code, 'deadline'); assert.equal(job.settlements, 1);
    await assert.rejects(proxy.request('/search', {}), error => error.code === 'poisoned');
    assert.equal(worker.terminations, 1); assert.equal(fatal(), 1); assert.equal(worker.sent.length, 1);
  });
  await run('never-started expiry acknowledgement preserves owner', async ({ make, advance, wallNow }) => {
    const { worker, proxy, fatal } = make(); worker.emit('message', { ready: true, manifest: initial }); await flush();
    const job = observe(proxy.request('/search', { deadline_at_ms: wallNow() + 10 }));
    const id = worker.sent[0].id; await advance(10); assert.equal(proxy.capabilities().queue.pending, 1);
    worker.emit('message', { id, error: 'deadline' }); await flush();
    assert.equal(job.code, 'deadline'); assert.equal(proxy.capabilities().queue.pending, 0);
    assert.equal(proxy.capabilities().ok, true); assert.equal(worker.terminations, 0); assert.equal(fatal(), 0);
  });
  await run('late start after queued expiry poisons owner', async ({ make, advance, wallNow }) => {
    const { worker, proxy, fatal } = make(); worker.emit('message', { ready: true, manifest: initial }); await flush();
    const job = observe(proxy.request('/search', { deadline_at_ms: wallNow() + 10 }));
    const id = worker.sent[0].id; await advance(10); worker.emit('message', { started: String(id) }); await flush();
    assert.equal(job.code, 'deadline'); assert.equal(worker.terminations, 1); assert.equal(fatal(), 1);
    assert.equal(proxy.capabilities().ok, false);
  });
  await run('exit after ready rejects pending job', async ({ make, wallNow }) => {
    const { worker, proxy, ready } = make(); worker.emit('message', { ready: true, manifest: initial }); await flush();
    const job = observe(proxy.request('/search', { deadline_at_ms: wallNow() + 10 })); worker.emit('exit', 1); await flush();
    assert.equal(ready.state, 'fulfilled'); assert.equal(job.code, 'poisoned'); assert.equal(job.settlements, 1);
  });
  await run('close exposes no native reap or replacement acknowledgement', async ({ make }) => {
    const { worker, proxy, ready } = make(); assert.equal(proxy.close(), undefined); await flush();
    assert.equal(ready.code, 'closed'); assert.equal(worker.terminations, 1); assert.equal(worker.sent.length, 0);
    await assert.rejects(proxy.request('/search', {}), error => error.code === 'poisoned');
  });
  await run('default budget begins at proxy creation', async ({ make, advance }) => {
    await advance(30); const { worker, ready } = make(); await advance(5);
    worker.emit('message', { ready: true, manifest: initial }); await flush(); assert.equal(ready.state, 'fulfilled');
  });
  await run('ready at deadline rejects even before timer callback', async ({ make, advance }) => {
    const { worker, proxy, ready } = make(); await advance(20, false);
    worker.emit('message', { ready: true, manifest: late }); await flush();
    assert.equal(ready.code, 'deadline'); assert.equal(proxy.capabilities().index, undefined);
  });
  await run('delayed timer cannot accept late ready', async ({ make, advance }) => {
    const { worker, ready } = make(); await advance(21, false);
    worker.emit('message', { ready: true, manifest: initial }); await flush(); assert.equal(ready.code, 'deadline');
  });
  await run('wall-clock jumps do not extend or expire monotonic startup', async ({ make, advance, changeWall }) => {
    const { worker, ready } = make(); changeWall(3600000); await advance(5);
    worker.emit('message', { ready: true, manifest: initial }); await flush(); assert.equal(ready.state, 'fulfilled');
    const next = make(); changeWall(-7200000); await advance(20, false);
    next.worker.emit('message', { ready: true, manifest: initial }); await flush(); assert.equal(next.ready.code, 'deadline');
  });
  await t.test('close before any ready consumer does not cause unhandled rejection', async child => {
    class FakeWorker extends EventEmitter { terminate() { return new Promise(() => {}); } }
    const unhandled = [];
    const listener = error => unhandled.push(error);
    process.on('unhandledRejection', listener);
    const proxy = createRuntimeProxy(new FakeWorker()); proxy.close();
    try {
      await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(unhandled, []);
      await assert.rejects(proxy.ready, error => error.code === 'closed');
    } finally { process.off('unhandledRejection', listener); proxy.close(); }
  });
});

test('resolved reranker with no finite scores does not claim readiness', async () => {
  const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
    llm: { rerank: async () => ({ model: rerankModel, results: [] }) } });
  const result = await service.request('/rerank', { passages: [{ file: row.filepath, passage_id: 'p1', revision: 'r1', text: row.body }] });
  assert.equal(result.manifest.rerank_ready, false);
  assert.deepEqual(result.scored_passage_ids, []);
  assert.equal(result.rerank_status, 'no_valid_scores');
  assert.equal(result.coverage, 'no_valid_scores');
  assert.ok(result.manifest.degradation.includes('rerank_no_scores_observed'));
  service.close();
});

const rankedPassages = () => ['a', 'b'].map(id => ({ file: `qmd://public/${id}.md`, passage_id: `passage-${id}`,
  revision: `synthetic-revision-${id}`, text: `Public ${id}: ${row.body}` }));
const rankRequest = () => ({ question: 'retry idempotency', passages: rankedPassages() });
const rankRows = docs => docs.map((doc, i) => ({ file: doc.file, score: i ? .9 : .5 }));

test('explicit SDK fallback rejects finite constant scores and does not remain ready after prior success', async () => {
  let mode = 'normal', calls = 0;
  const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
    llm: { rerank: async (_, docs) => {
      calls++;
      return mode === 'fallback'
        ? { model: 'fallback', results: docs.map(doc => ({ ...doc, score: .5, index: 0 })) }
        : { model: rerankModel, results: rankRows(docs) };
    } } });
  try {
    const first = await service.request('/rerank', rankRequest());
    assert.equal(first.rerank_status, 'scored');
    assert.equal(first.model, rerankModel);
    assert.equal(first.coverage, 'exact_passage');
    assert.equal(first.passage_scores['passage-a'], .5); // A legitimate .5 is accepted.
    assert.equal(first.manifest.rerank_ready, true);
    mode = 'fallback';
    const fallback = await service.request('/rerank', rankRequest());
    assert.equal(fallback.rerank_status, 'sdk_fallback');
    assert.equal(fallback.model, 'fallback');
    assert.equal(fallback.coverage, 'sdk_fallback_no_rerank');
    assert.deepEqual(fallback.scores, {});
    assert.deepEqual(fallback.passage_scores, {});
    assert.deepEqual(fallback.scored_passage_ids, []);
    assert.equal(fallback.manifest.rerank_ready, false);
    assert.equal(fallback.manifest.models.rerank.ready, false);
    assert.equal(service.capabilities().rerank_status, 'sdk_fallback');
    assert.ok(fallback.manifest.degradation.includes('rerank_sdk_fallback'));
    mode = 'normal';
    const recovered = await service.request('/rerank', rankRequest());
    assert.equal(recovered.rerank_status, 'scored');
    assert.equal(recovered.manifest.rerank_ready, true);
    assert.equal(recovered.manifest.models.rerank.ready, true);
    assert.ok(recovered.manifest.degradation.includes('rerank_sdk_fallback'));
    assert.equal(calls, 3);
  } finally { service.close(); }
});

test('missing, malformed and wrong returned model IDs cannot confirm passage scoring', async () => {
  for (const model of [undefined, null, false, [], {}, 'another-model', 'unknown', 'x'.repeat(2048)]) {
    const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
      llm: { rerank: async (_, docs) => ({ model, results: rankRows(docs) }) } });
    try {
      const result = await service.request('/rerank', rankRequest());
      assert.equal(result.rerank_status, 'unverified_model');
      assert.equal(result.coverage, 'unverified_model');
      assert.equal(result.model, 'unknown'); // Never echo an arbitrary adapter model string.
      assert.deepEqual(result.passage_scores, {});
      assert.deepEqual(result.scores, {});
      assert.deepEqual(result.scored_passage_ids, []);
      assert.equal(result.manifest.rerank_ready, false);
      assert.equal(result.manifest.models.rerank.ready, false);
    } finally { service.close(); }
  }
});

test('unbounded or unverified configured model identity prevents the SDK call', async () => {
  for (const id of [undefined, '', false, {}, 'fallback', 'unknown', ' trailing ', 'bad\nmodel',
    '\uD800', '\uDC00', 'é'.repeat(513)]) {
    let calls = 0;
    const service = createRecallService({ store: {}, lexical: lex(), manifest: { models: { rerank: { id } } },
      llm: { rerank: async (_, docs) => { calls++; return { model: id, results: rankRows(docs) }; } } });
    try {
      const result = await service.request('/rerank', rankRequest());
      assert.equal(result.rerank_status, 'unverified_model');
      assert.equal(result.model, 'unknown');
      assert.equal(result.manifest.rerank_ready, false);
      assert.deepEqual(result.scored_passage_ids, []);
      assert.equal(calls, 0);
    } finally { service.close(); }
  }
});

test('invalid score records reject the whole score result instead of accepting a convincing subset', async () => {
  const invalidResults = [undefined, null, {}, [null], [{ file: 'unknown-passage', score: .8 }],
    [{ file: 'passage-a', score: '0.8' }], [{ file: 'passage-a', score: true }],
    [{ file: 'passage-a', score: NaN }], [{ file: 'passage-a', score: Infinity }],
    [{ file: 'passage-a', score: .8 }, { file: 'passage-a', score: .8 }],
    [{ file: 'passage-a', score: .8 }, { file: 'passage-a', score: .1 }],
    [{ file: 'passage-a', score: .8 }, { file: 'passage-b', score: NaN }]];
  for (const results of invalidResults) {
    const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
      llm: { rerank: async () => ({ model: rerankModel, results }) } });
    try {
      const result = await service.request('/rerank', rankRequest());
      assert.equal(result.rerank_status, 'invalid_scores');
      assert.equal(result.coverage, 'invalid_scores');
      assert.equal(result.model, rerankModel);
      assert.deepEqual(result.passage_scores, {});
      assert.deepEqual(result.scores, {});
      assert.deepEqual(result.scored_passage_ids, []);
      assert.equal(result.manifest.rerank_ready, false);
      assert.equal(result.manifest.models.rerank.ready, false);
    } finally { service.close(); }
  }
});

test('partial matching-model scores remain partial and clear current readiness', async () => {
  let partial = false;
  const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
    llm: { rerank: async (_, docs) => ({ model: rerankModel, results: rankRows(partial ? docs.slice(0, 1) : docs) }) } });
  try {
    assert.equal((await service.request('/rerank', rankRequest())).manifest.rerank_ready, true);
    partial = true;
    const result = await service.request('/rerank', rankRequest());
    assert.equal(result.rerank_status, 'partial_scores');
    assert.equal(result.coverage, 'partial_passage_scores');
    assert.deepEqual(result.passage_scores, { 'passage-a': .5 });
    assert.deepEqual(result.scored_passage_ids, ['passage-a']);
    assert.equal(result.manifest.rerank_ready, false);
    assert.equal(result.manifest.models.rerank.ready, false);
  } finally { service.close(); }
});

test('cache absence, empty requests and failed model operations do not inherit earlier readiness', async () => {
  let available = true, fail = false, calls = 0;
  const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
    modelAvailable: () => available, llm: { rerank: async (_, docs) => {
      calls++; if (fail) throw new Error('Synthetic adapter failure');
      return { model: rerankModel, results: rankRows(docs) };
    } } });
  try {
    assert.equal((await service.request('/rerank', rankRequest())).manifest.rerank_ready, true);
    available = false;
    const missing = await service.request('/rerank', rankRequest());
    assert.equal(missing.rerank_status, 'model_cache_unavailable');
    assert.equal(missing.coverage, 'model_cache_unavailable');
    assert.equal(missing.model, 'unknown');
    assert.equal(missing.manifest.rerank_ready, false);
    assert.deepEqual(missing.passage_scores, {});
    available = true;
    assert.equal((await service.request('/rerank', rankRequest())).manifest.rerank_ready, true);
    const empty = await service.request('/rerank', { passages: [] });
    assert.equal(empty.rerank_status, 'not_requested');
    assert.equal(empty.coverage, 'no_passages');
    assert.equal(empty.manifest.rerank_ready, false);
    assert.deepEqual(empty.passage_scores, {});
    fail = true;
    await assert.rejects(service.request('/rerank', rankRequest()), /Synthetic adapter failure/);
    assert.equal(service.capabilities().rerank_status, 'failed');
    assert.equal(service.capabilities().rerank_ready, false);
    assert.equal(calls, 3);
  } finally { service.close(); }
});

test('duplicate or unversioned requested passages fail before scoring and clear readiness', async () => {
  let calls = 0;
  const service = createRecallService({ store: {}, lexical: lex(), manifest: rerankManifest,
    llm: { rerank: async (_, docs) => { calls++; return { model: rerankModel, results: rankRows(docs) }; } } });
  try {
    assert.equal((await service.request('/rerank', rankRequest())).manifest.rerank_ready, true);
    const first = rankedPassages()[0];
    for (const passages of [[first, first], [{ ...first, revision: undefined }],
      [{ ...first, revision: {} }], [{ ...first, passage_id: 1 }], [null]]) {
      await assert.rejects(service.request('/rerank', { passages }), e => e.code === 'invalid_passage');
      assert.equal(service.capabilities().rerank_ready, false);
      assert.equal(service.capabilities().rerank_status, 'invalid_passage');
    }
    assert.equal(calls, 1);
  } finally { service.close(); }
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

test('metadata ownership: pending refresh settles before a later scoped search starts', async () => {
  const held = deferred(), started = deferred(), calls = [];
  const service = createRecallService({ store: {}, llm: {}, modelAvailable: () => false,
    lexical: { async request(message) {
      calls.push(message.kind);
      if (message.kind === 'metadata') { started.resolve(); await held.promise; }
      return { rows: [row], metadata };
    }, close() {} } });
  let search, refresh;
  try {
    refresh = service.refresh(Date.now() + 1000);
    await started.promise;
    search = service.request('/search', { rungs: ['retry'], collections: ['public'] });
    await new Promise(setImmediate);
    assert.deepEqual(calls, ['metadata']);
    held.resolve(); await refresh;
    const result = await search;
    assert.deepEqual(calls, ['metadata', 'search']);
    assert.equal(result.coverage.lexical, 'complete_scoped_before_limit');
    assert.equal(result.lex[0][0].file, row.filepath);
  } finally { held.resolve(); await Promise.allSettled([refresh, search]); service.close(); }
});

test('metadata ownership: real lexical metadata timeout cannot interrupt a later longer search', async () => {
  const workers = []; let fallback = 0;
  class FixtureWorker extends EventEmitter {
    sent = [];
    constructor(held) { super(); this.held = held; }
    postMessage(message) {
      this.sent.push(message.kind);
      if (!this.held) queueMicrotask(() => this.emit('message', { id: message.id, rows: [row], metadata }));
    }
    terminate() { queueMicrotask(() => this.emit('exit', 0)); return Promise.resolve(); }
  }
  const scoped = new ScopedLexicalWorker({});
  const lexical = {
    request(message, deadline) {
      if (!scoped.worker) { const worker = new FixtureWorker(workers.length === 0); workers.push(worker); scoped.worker = worker; }
      return scoped.request(message, deadline);
    },
    close() { scoped.close(); },
  };
  const service = createRecallService({ lexical, store: {}, llm: {}, modelAvailable: () => false, metadataTimeoutMs: 20,
    fallbackLexical: async () => { fallback++; return [row]; } });
  try {
    const refresh = service.refresh(Date.now() + 20);
    await new Promise(setImmediate);
    const search = service.request('/search', { rungs: ['retry'], collections: ['public'], deadline_at_ms: Date.now() + 1000 });
    await refresh;
    const result = await search;
    assert.equal(fallback, 0);
    assert.equal(result.coverage.lexical, 'complete_scoped_before_limit');
    assert.equal(result.lex[0][0].file, row.filepath);
    assert.deepEqual(workers.map(worker => worker.sent), [['metadata'], ['search']]);
    assert.ok(service.capabilities().degradation.includes('deadline'));
    assert.equal(workers[0].listenerCount('message'), 0);
    assert.equal(workers[0].listenerCount('exit'), 0);
  } finally { service.close(); }
});

test('metadata ownership: a shorter coalesced waiter cannot cancel the shared refresh', async () => {
  const held = deferred(), started = deferred(); let calls = 0, closed = 0;
  const service = createRecallService({ store: {}, llm: {}, lexical: {
    async request() { calls++; started.resolve(); await held.promise; return { rows: [], metadata }; },
    close() { closed++; },
  } });
  let first, second;
  try {
    first = service.refresh(Date.now() + 1000); await started.promise;
    second = service.refresh(Date.now() + 15);
    await Promise.race([second, sleep(150).then(() => { throw new Error('Short metadata waiter did not finish'); })]);
    assert.equal(calls, 1); assert.equal(closed, 0);
    assert.equal(service.capabilities().lexical_ready, false);
    held.resolve(); await first;
    assert.equal(service.capabilities().lexical_ready, true);
  } finally { held.resolve(); await Promise.allSettled([first, second]); service.close(); }
});

test('metadata ownership: the first short health waiter cannot set the shared borrow deadline', async () => {
  const held = deferred(), started = deferred(); let calls = 0, closed = 0, borrowDeadline;
  const began = Date.now();
  const service = createRecallService({ store: {}, llm: {}, lexical: {
    async request(message, deadline) {
      calls++; borrowDeadline = deadline; started.resolve(); await held.promise; return { rows: [], metadata };
    },
    close() { closed++; },
  } });
  let short, longer;
  try {
    assert.equal((await service.refresh(Date.now() - 1)).lexical_ready, false);
    assert.equal(calls, 0);
    short = service.refresh(Date.now() + 15); await started.promise;
    const pending = await Promise.race([short, sleep(150).then(() => { throw new Error('Short health waiter did not finish'); })]);
    assert.equal(pending.lexical_ready, false); assert.equal(closed, 0);
    assert.ok(borrowDeadline >= began + 2900 && borrowDeadline <= Date.now() + 3000);
    assert.equal((await service.refresh(Date.now() + 15)).lexical_ready, false);
    assert.equal(calls, 1); assert.equal(closed, 0);
    longer = service.refresh(Date.now() + 1000);
    held.resolve();
    assert.equal((await longer).lexical_ready, true);
    assert.equal(calls, 1); assert.equal(closed, 0);
    assert.equal(service.capabilities().degradation.includes('deadline'), false);
  } finally { held.resolve(); await Promise.allSettled([short, longer]); service.close(); }
});

test('metadata ownership: an unresolved adapter is bounded and late success is ignored', async () => {
  const held = deferred(); let closed = 0;
  const service = createRecallService({ store: {}, llm: {}, metadataTimeoutMs: 15, lexical: {
    request() { return held.promise; }, close() { closed++; },
  } });
  try {
    const result = await Promise.race([service.refresh(Date.now() + 1000), sleep(200).then(() => { throw new Error('Unbounded refresh'); })]);
    assert.equal(result.lexical_ready, false); assert.equal(closed, 1);
    assert.ok(result.degradation.includes('deadline'));
    held.resolve({ rows: [], metadata }); await new Promise(setImmediate);
    assert.equal(service.capabilities().lexical_ready, false);
  } finally { held.resolve({ rows: [], metadata }); service.close(); }
});

test('metadata ownership: synchronous and rejected adapters release the refresh boundary', async () => {
  for (const reject of [false, true]) {
    let calls = 0;
    const service = createRecallService({ store: {}, llm: {}, modelAvailable: () => false,
      lexical: { request(message) {
        calls++;
        if (message.kind === 'metadata') {
          const error = Object.assign(new Error('Synthetic metadata failure'), { code: 'fixture_failure' });
          if (reject) return Promise.reject(error);
          throw error;
        }
        return Promise.resolve({ rows: [row], metadata });
      }, close() {} } });
    try {
      const health = await service.refresh(Date.now() + 1000);
      assert.ok(health.degradation.includes('fixture_failure'));
      const result = await service.request('/search', { rungs: ['retry'] });
      assert.equal(result.lex[0][0].file, row.filepath); assert.equal(calls, 2);
    } finally { service.close(); }
  }
});

test('metadata ownership: malformed response and throwing cleanup cannot strand waiters', async () => {
  for (const malformed of [true, false]) {
    const service = createRecallService({ store: {}, llm: {}, metadataTimeoutMs: 15, lexical: {
      request() { return malformed ? Promise.resolve(undefined) : new Promise(() => {}); },
      close() { throw Object.assign(new Error('Synthetic close failure'), { code: 'fixture_close_failure' }); },
    } });
    try {
      const health = await Promise.race([service.refresh(Date.now() + 1000), sleep(100).then(() => { throw new Error('Stranded metadata waiter'); })]);
      assert.equal(health.lexical_ready, false);
      assert.ok(health.degradation.includes(malformed ? 'index_metadata_unavailable' : 'deadline'));
      service.close();
      assert.equal(service.capabilities().ok, false);
      assert.ok(service.capabilities().degradation.includes('fixture_close_failure'));
    } finally { service.close(); }
  }
});

test('metadata ownership: queued deadline and cancellation are rechecked after waiting', async () => {
  for (const cancel of [false, true]) {
    const held = deferred(), started = deferred(); let searched = 0, cancelled = false;
    const service = createRecallService({ store: {}, llm: {}, modelAvailable: () => false,
      lexical: { async request(message) {
        if (message.kind === 'metadata') { started.resolve(); await held.promise; }
        else searched++;
        return { rows: [row], metadata };
      }, close() {} } });
    let refresh, query;
    try {
      refresh = service.refresh(Date.now() + 1000); await started.promise;
      query = service.request('/search', { rungs: ['retry'], deadline_at_ms: Date.now() + (cancel ? 1000 : 15) }, () => cancelled);
      const outcome = query.then(() => 'unexpected success', error => error.code);
      if (cancel) { cancelled = true; held.resolve(); }
      assert.equal(await outcome, 'deadline');
      held.resolve(); await refresh;
      await new Promise(setImmediate);
      assert.equal(searched, 0);
    } finally { held.resolve(); await Promise.allSettled([refresh, query]); service.close(); }
  }
});

test('metadata ownership: close settles refresh and queued callers and rejects new work', async () => {
  const held = deferred(), started = deferred(); let searched = 0;
  const service = createRecallService({ store: {}, llm: {}, modelAvailable: () => false,
    lexical: { async request(message) {
      if (message.kind === 'metadata') { started.resolve(); await held.promise; }
      else searched++;
      return { rows: [row], metadata };
    }, close() {} } });
  let refresh, query;
  try {
    refresh = service.refresh(Date.now() + 1000); await started.promise;
    query = service.request('/search', { rungs: ['retry'] });
    const outcome = query.then(() => 'unexpected success', error => error.code);
    service.close(); service.close();
    const health = await Promise.race([refresh, sleep(100).then(() => { throw new Error('Close did not settle refresh'); })]);
    assert.equal(health.ok, false); assert.equal(await outcome, 'closed');
    assert.equal(searched, 0);
    await assert.rejects(service.request('/search', { rungs: ['retry'] }), error => error.code === 'closed');
    held.resolve(); await new Promise(setImmediate);
    assert.equal(service.capabilities().lexical_ready, false);
  } finally { held.resolve(); await Promise.allSettled([refresh, query]); service.close(); }
});

test('metadata ownership: close rejects active caller without admitting another model', async () => {
  const held = deferred(), started = deferred(); let models = 0;
  const service = createRecallService({ lexical: lex(), llm: {}, store: {
    async searchVector() { models++; started.resolve(); await held.promise; return [row]; },
  } });
  let active;
  try {
    active = service.request('/search', { question: 'owned model', deadline_at_ms: Date.now() + 1000 });
    const outcome = active.then(() => 'unexpected success', error => error.code);
    await started.promise; service.close();
    const result = await Promise.race([outcome, sleep(100).then(() => 'close did not settle caller')]);
    assert.equal(result, 'closed');
    await assert.rejects(service.request('/search', { question: 'new' }), error => error.code === 'closed');
    assert.equal(models, 1);
    held.resolve(); await new Promise(setImmediate);
    assert.equal(service.capabilities().ok, false);
  } finally { held.resolve(); await Promise.allSettled([active]); service.close(); }
});

test('metadata ownership: close during lexical work starts no fallback, next rung or vector', async () => {
  for (const failed of [false, true]) {
    const held = deferred(), started = deferred(); let lexicalCalls = 0, fallback = 0, models = 0;
    const service = createRecallService({ llm: {},
      lexical: { request() { lexicalCalls++; started.resolve(); return held.promise; },
        close() {
          if (failed) held.reject(Object.assign(new Error('Closed fixture'), { code: 'adapter_unavailable' }));
          else held.resolve({ rows: [row], metadata });
        } },
      fallbackLexical: async () => { fallback++; return [row]; },
      store: { async searchVector() { models++; return [row]; } },
    });
    try {
      const query = service.request('/search', { rungs: ['retry', 'payment'], question: 'retry payment' });
      const outcome = query.then(() => 'unexpected success', error => error.code);
      await started.promise; service.close();
      assert.equal(await outcome, 'closed');
      await new Promise(setImmediate);
      assert.equal(lexicalCalls, 1); assert.equal(fallback, 0); assert.equal(models, 0);
    } finally { service.close(); }
  }
});
