// QMD 2.8.3 metadata observer. No createStore, migrations, model/runtime calls,
// downloads, or indexing. The parent enforces a process/output deadline.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {dirname, join, isAbsolute} from 'node:path';
import {openSync, fstatSync, readSync, closeSync} from 'node:fs';
import {createHash} from 'node:crypto';

const PIN = '2.8.3';
const INPUT_LIMIT = 1024 * 1024;
const MAX_BODY = 1024 * 1024;
const MAX_TOTAL = 8 * 1024 * 1024;
const MAX_CHUNKS = 16384;
const SQLITE_HEAP_BYTES = 16 * 1024 * 1024;
const FAILURE_CODES = new Set([
  'proof_request_invalid', 'proof_metadata_unsupported', 'proof_sdk_unsupported',
  'proof_schema_unsupported', 'proof_sqlite_bound_unavailable',
  'proof_revision_absent', 'proof_revision_ambiguous', 'proof_revision_inactive',
  'proof_revision_mismatch', 'proof_title_bound', 'proof_content_bound',
  'proof_total_content_bound', 'proof_content_hash_mismatch', 'proof_fts_bound',
  'proof_fts_mismatch', 'proof_model_config_unsupported', 'proof_vector_schema_unsupported',
  'proof_vector_chunks_incomplete', 'proof_vector_chunk_bound', 'proof_vector_pipeline_mismatch',
  'proof_vector_storage_invalid', 'proof_vector_nonfinite', 'proof_vector_zero',
]);
// Only errors created here carry a diagnostic code. Dependency errors cannot
// claim a trusted code by copying an error name, message, property or prototype.
const failures = new WeakMap();
class ProofFailure extends Error {
  constructor(code) {
    super('Read-only index proof failed');
    failures.set(this, FAILURE_CODES.has(code) ? code : 'proof_internal_failure');
  }
}
const fail = code => { throw new ProofFailure(code); };
export const failureReason = error => failures.get(error) || 'proof_internal_failure';
const result = (lexical = false, vector = false, reason = '') => ({lexical, vector, reason});

function boundedFile(path, limit) {
  const fd = openSync(path, 'r');
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.size > limit) fail('proof_metadata_unsupported');
    const body = Buffer.alloc(Math.min(info.size + 1, limit + 1));
    let n = 0, count;
    while (n < body.length && (count = readSync(fd, body, n, body.length - n, null)) > 0) n += count;
    if (n > limit || n !== info.size) fail('proof_metadata_unsupported');
    return body.subarray(0, n).toString('utf8');
  } finally { closeSync(fd); }
}

function columns(db, table, required) {
  const definition = db.prepare('SELECT type,sql FROM sqlite_master WHERE name=?').get(table);
  if (definition?.type !== 'table') fail('proof_schema_unsupported');
  const names = new Set(db.pragma(`table_info(${table})`).map(c => c.name));
  if (!required.every(name => names.has(name))) fail('proof_schema_unsupported');
  return definition.sql || '';
}

export function capSqliteAllocator(db) {
  const heap = db.pragma(`hard_heap_limit=${SQLITE_HEAP_BYTES}`, {simple: true});
  if (!Number.isSafeInteger(heap) || heap <= 0 || heap > SQLITE_HEAP_BYTES)
    fail('proof_sqlite_bound_unavailable');
}

export async function prove(input) {
  let db, lexical = false;
  try {
    if (!input || typeof input !== 'object' || !isAbsolute(input.database || '')
        || !isAbsolute(input.sdk || '') || !Array.isArray(input.references)
        || !input.references.length || input.references.length > 1000 || typeof input.vector !== 'boolean')
      fail('proof_request_invalid');
    const pkg = JSON.parse(boundedFile(join(dirname(input.sdk), '..', 'package.json'), 65536));
    if (pkg.name !== '@tobilu/qmd' || pkg.version !== PIN) fail('proof_sdk_unsupported');
    const requireSDK = createRequire(pathToFileURL(input.sdk));
    const Database = requireSDK('better-sqlite3');
    // These module imports expose only formatting/model-URI resolution. They
    // do not initialize llama.cpp or open/create any QMD store.
    const {getEmbeddingFingerprint, normalizeCjkForFTS} = await import(pathToFileURL(join(dirname(input.sdk), 'store.js')).href);
    const {resolveEmbedModel} = await import(pathToFileURL(join(dirname(input.sdk), 'llm.js')).href);
    db = new Database(input.database, {readonly: true, fileMustExist: true, timeout: 100});
    // Process-global SQLite allocation limit is safe here: production uses a
    // fresh disposable observer, never the recall server/indexing process.
    capSqliteAllocator(db);
    db.pragma('query_only = ON');
    db.exec('BEGIN');
    // better-sqlite3 can be built with SQLITE_DEFAULT_MEMSTATUS=0, which
    // weakens hard_heap_limit. octet_length on ordinary stored columns reads
    // their encoded size without materializing TEXT; an unsupported SQLite
    // build must fail before any content reads.
    if (db.prepare("SELECT octet_length('') AS n").get()?.n !== 0)
      fail('proof_sqlite_bound_unavailable');
    const documentSchema = columns(db, 'documents', ['id', 'collection', 'path', 'title', 'hash', 'active']);
    const contentSchema = columns(db, 'content', ['hash', 'doc']);
    if (/\bUSING\b/i.test(documentSchema) || /\bUSING\b/i.test(contentSchema))
      fail('proof_schema_unsupported');
    const fts = columns(db, 'documents_fts', ['filepath', 'title', 'body']);
    if (!/\bUSING\s+fts5\s*\(/i.test(fts) || /\bcontent\s*=/i.test(fts)) fail('proof_schema_unsupported');
    const shadow = columns(db, 'documents_fts_content', ['id', 'c0', 'c1', 'c2']);
    const layout = db.pragma('table_info(documents_fts_content)');
    if (/\bUSING\b/i.test(shadow) || layout.length !== 4 || layout[0].name !== 'id'
        || layout[0].type.toUpperCase() !== 'INTEGER' || layout[0].pk !== 1
        || !['c0', 'c1', 'c2'].every((name, i) => layout[i + 1].name === name
          && layout[i + 1].type === '' && layout[i + 1].pk === 0))
      fail('proof_schema_unsupported');
    const documents = db.prepare('SELECT id,hash,octet_length(title) AS title_length,active FROM documents WHERE collection=? AND path=? LIMIT 2');
    const titles = db.prepare('SELECT title FROM documents WHERE id=?');
    const lengths = db.prepare('SELECT octet_length(doc) AS n FROM content WHERE hash=? LIMIT 2');
    const contents = db.prepare('SELECT doc FROM content WHERE hash=?');
    const ftsRows = db.prepare('SELECT filepath,title,body FROM documents_fts WHERE rowid=? LIMIT 2');
    // A virtual FTS xColumn would materialize its value before octet_length.
    // The pinned standalone FTS5 shadow content table is ordinary storage.
    const ftsLengths = db.prepare('SELECT octet_length(c0) AS path_length,octet_length(c1) AS title_length,'
      + 'octet_length(c2) AS body_length FROM documents_fts_content WHERE id=? LIMIT 2');
    const hashes = new Set(), names = new Set();
    let bytes = 0;
    for (const ref of input.references) {
      if (!ref || Object.keys(ref).sort().join(',') !== 'collection,path,revision'
          || typeof ref.collection !== 'string' || !/^[A-Za-z0-9_.-]{1,200}$/.test(ref.collection)
          || typeof ref.path !== 'string' || !ref.path || Buffer.byteLength(ref.path) > 4096
          || /[\x00-\x1f]/.test(ref.path) || isAbsolute(ref.path)
          || ref.path.split('/').some(part => ['', '.', '..'].includes(part))
          || typeof ref.revision !== 'string' || !/^[a-f0-9]{64}$/.test(ref.revision))
        fail('proof_request_invalid');
      const name = JSON.stringify([ref.collection, ref.path]);
      if (names.has(name)) fail('proof_request_invalid');
      names.add(name);
      const rows = documents.all(ref.collection, ref.path);
      if (!rows.length) fail('proof_revision_absent');
      if (rows.length !== 1) fail('proof_revision_ambiguous');
      if (rows[0].active !== 1) fail('proof_revision_inactive');
      if (rows[0].hash !== ref.revision) fail('proof_revision_mismatch');
      if (!Number.isSafeInteger(rows[0].title_length) || rows[0].title_length < 0 || rows[0].title_length > 16384)
        fail('proof_title_bound');
      const title = titles.get(rows[0].id)?.title;
      const length = lengths.all(ref.revision);
      if (length.length !== 1 || !Number.isSafeInteger(length[0].n) || length[0].n <= 0 || length[0].n > MAX_BODY)
        fail('proof_content_bound');
      bytes += length[0].n;
      if (bytes > MAX_TOTAL) fail('proof_total_content_bound');
      const body = contents.get(ref.revision)?.doc;
      if (typeof body !== 'string' || createHash('sha256').update(body).digest('hex') !== ref.revision)
        fail('proof_content_hash_mismatch');
      const expected = [ref.collection + '/' + ref.path, title, body];
      const sizes = ftsLengths.all(rows[0].id);
      if (sizes.length !== 1 || ![['path_length', 16384], ['title_length', 16384], ['body_length', MAX_BODY]]
          .every(([key, bound]) => Number.isSafeInteger(sizes[0][key]) && sizes[0][key] >= 0 && sizes[0][key] <= bound))
        fail('proof_fts_bound');
      const entries = ftsRows.all(rows[0].id);
      const actual = entries.length === 1 ? [entries[0].filepath, entries[0].title, entries[0].body] : [];
      if (!expected.every((value, i) => actual[i] === value)
          && !expected.every((value, i) => actual[i] === normalizeCjkForFTS(value)))
        fail('proof_fts_mismatch');
      hashes.add(ref.revision);
    }
    lexical = true;
    if (!input.vector) return result(true);
    if (!isAbsolute(input.configuration || '')) fail('proof_model_config_unsupported');
    const YAML = requireSDK('yaml');
    const config = YAML.parse(boundedFile(input.configuration, 256000), {maxAliasCount: 100, uniqueKeys: true});
    if (!config || typeof config !== 'object' || Array.isArray(config)
        || (config.models !== undefined && (!config.models || typeof config.models !== 'object' || Array.isArray(config.models)))
        || (config.models?.embed !== undefined && (typeof config.models.embed !== 'string' || !config.models.embed)))
      fail('proof_model_config_unsupported');
    const model = resolveEmbedModel(config.models);
    if (typeof model !== 'string' || model.length > 4096) fail('proof_model_config_unsupported');
    const fingerprint = getEmbeddingFingerprint(model);
    columns(db, 'content_vectors', ['hash', 'seq', 'pos', 'model', 'embed_fingerprint', 'total_chunks']);
    requireSDK('sqlite-vec').load(db);
    const vecSchema = columns(db, 'vectors_vec', ['hash_seq', 'embedding']);
    const dimension = /\bembedding\s+float\s*\[\s*(\d+)\s*\]/i.exec(vecSchema);
    if (!/\bUSING\s+vec0\s*\(/i.test(vecSchema) || !dimension) fail('proof_vector_schema_unsupported');
    const dimensions = Number(dimension[1]);
    if (!Number.isSafeInteger(dimensions) || dimensions < 1 || dimensions > 16384) fail('proof_vector_schema_unsupported');
    const metadata = db.prepare('SELECT seq,pos,model,embed_fingerprint,total_chunks FROM content_vectors WHERE hash=? ORDER BY seq LIMIT ?');
    // Never JOIN a sqlite-vec virtual table: pinned native builds can hang.
    const vectors = db.prepare('SELECT embedding FROM vectors_vec WHERE hash_seq=? LIMIT 2');
    let chunks = 0;
    for (const hash of hashes) {
      const rows = metadata.all(hash, MAX_CHUNKS + 1);
      const expected = rows[0]?.total_chunks;
      if (!Number.isSafeInteger(expected) || expected < 1 || expected > MAX_CHUNKS || rows.length !== expected)
        fail('proof_vector_chunks_incomplete');
      chunks += rows.length;
      if (chunks > MAX_CHUNKS) fail('proof_vector_chunk_bound');
      for (let seq = 0; seq < rows.length; seq++) {
        const row = rows[seq];
        if (row.seq !== seq || !Number.isSafeInteger(row.pos) || row.pos < 0
            || row.total_chunks !== expected || row.model !== model || row.embed_fingerprint !== fingerprint)
          fail('proof_vector_pipeline_mismatch');
        const stored = vectors.all(`${hash}_${seq}`);
        const blob = stored[0]?.embedding;
        if (stored.length !== 1 || !Buffer.isBuffer(blob) || blob.length !== dimensions * 4)
          fail('proof_vector_storage_invalid');
        let nonzero = false;
        for (let i = 0; i < dimensions; i++) {
          const value = blob.readFloatLE(i * 4);
          if (!Number.isFinite(value)) fail('proof_vector_nonfinite');
          nonzero ||= value !== 0;
        }
        if (!nonzero) fail('proof_vector_zero');
      }
    }
    return result(true, true);
  } catch (error) {
    // Dependency/native diagnostics and file/document/model strings never go
    // into the protocol. The parent receives metadata status only.
    return result(lexical, false, failureReason(error));
  } finally { if (db) db.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let input = Buffer.alloc(0);
  try {
    for await (const chunk of process.stdin) {
      if (input.length + chunk.length > INPUT_LIMIT) fail('proof_request_invalid');
      input = Buffer.concat([input, chunk]);
    }
    process.stdout.write(JSON.stringify(await prove(JSON.parse(input.toString('utf8')))) + '\n');
  } catch { process.stdout.write(JSON.stringify(result(false, false, 'proof_request_invalid')) + '\n'); }
}
