// claude-note recall bridge: QMD's own keyword search, vector search and reranker in ONE process.
//
// Recall used to spawn `qmd search` once per rung per collection (up to ~12 processes, each opening
// the index), which made the slowest prompts wait 10 s or more. This runs every rung in one process,
// adds a vector search of the whole question (no query expansion: that loads a 1.3 GB model), and can
// rerank the strongest candidates with QMD's small reranker. Python owns the query ladder, fusion and
// presentation (claude_note/recall.py); this file only fetches.
//
// usage: bun qmd_bridge.ts <qmd-src-dir> <index-db-path>   (JSON on stdin, JSON on stdout)
// stdin:  {"requests": [{"rungs": [["run","npm","box"], ...], "scopes": [null | "notes", ...],
//           "search_n": 10, "vector": "the whole question" | "", "vector_k": 30,
//           "rerank": 0 | N, "rerank_query": "..."}]}
// stdout: {"results": [{"fts": [[scope_i, rung_i, [hits]], ...], "vec": [hits], "rerank": {file: score},
//           "ms": {...}}], "load_ms": n}
// A hit is {file, title, score, body?}. Failures inside one request are reported, never thrown.

const [src, dbPath] = process.argv.slice(2);
const t0 = performance.now();
const { createStore, searchFTS, searchVec, DEFAULT_EMBED_MODEL } = await import(`${src}/store.ts`);
const { withLLMSession } = await import(`${src}/llm.ts`);
const input = JSON.parse(await Bun.stdin.text());
const store = createStore(dbPath);
const results: any[] = [];
const BODY = 2400;
const hit = (r: any, withBody = false) => ({ file: r.filepath, title: r.title, score: Math.round(r.score * 10000) / 10000,
  ...(withBody ? { body: (r.body || '').slice(0, BODY) } : {}) });

const needsModel = (input.requests || []).some((r: any) => r.vector || r.rerank);
const run = async (session: any) => {
  for (const req of input.requests || []) {
    const out: any = { fts: [], vec: [], rerank: {}, ms: {} };
    const bodies = new Map<string, any>();
    try {
      let s = performance.now();
      const scopes = req.scopes && req.scopes.length ? req.scopes : [null];
      for (let si = 0; si < scopes.length; si++) {
        for (let ri = 0; ri < (req.rungs || []).length; ri++) {
          const got = searchFTS(store.db, req.rungs[ri].join(' '), req.search_n || 10, scopes[si] ?? undefined);
          for (const r of got) bodies.set(r.filepath, r);
          out.fts.push([si, ri, got.map((r: any) => hit(r))]);
        }
      }
      out.ms.fts = Math.round(performance.now() - s);
      if (req.vector && session) {
        s = performance.now();
        const got = await searchVec(store.db, req.vector, DEFAULT_EMBED_MODEL, req.vector_k || 30, undefined, session);
        for (const r of got) if (!bodies.has(r.filepath)) bodies.set(r.filepath, r);
        out.vec = got.map((r: any) => hit(r));
        out.ms.vec = Math.round(performance.now() - s);
      }
      if (req.rerank && session) {
        // Rerank the strongest candidates by a quick reciprocal-rank fusion of everything above.
        s = performance.now();
        const rrf = new Map<string, number>();
        for (const [, ri, hits] of out.fts) hits.forEach((h: any, i: number) => rrf.set(h.file, (rrf.get(h.file) || 0) + 1 / (60 + i + 3 * ri)));
        out.vec.forEach((h: any, i: number) => rrf.set(h.file, (rrf.get(h.file) || 0) + 1 / (60 + i)));
        const top = [...rrf.entries()].sort((a, b) => b[1] - a[1]).slice(0, req.rerank).map(([f]) => f);
        const docs = top.map((f) => { const r = bodies.get(f); return { file: f, title: r?.title || '', text: (r?.body || '').slice(0, BODY) }; });
        if (docs.length) {
          const rr = await session.rerank(req.rerank_query || req.vector, docs);
          for (const r of (rr.results || rr)) out.rerank[r.file] = Math.round(r.score * 10000) / 10000;
        }
        out.ms.rerank = Math.round(performance.now() - s);
      }
    } catch (e: any) {
      out.error = String(e && e.message || e).slice(0, 300);
    }
    results.push(out);
  }
};
try {
  if (needsModel) await withLLMSession(run, { maxDuration: 30 * 60 * 1000, name: 'claude-note-recall' });
  else await run(null);
} finally {
  store.close();
}
process.stdout.write(JSON.stringify({ results, load_ms: Math.round(performance.now() - t0) }));
process.exit(0);
