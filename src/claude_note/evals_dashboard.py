"""`claude-note eval dashboard`: one self-contained HTML file (no network) from the eval history.

Layout: a strip of metrics (choose one to see its trend across runs), what to fix next, the
failing cases with what recall injected instead, and the live signals from the real hook.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

METRICS = [
    # key, label, group, format, higher is better, help
    ("hit_at_k", "Right note injected", "test", "pct", True, "Share of questions whose answering note was among the notes recall injected (test split)."),
    ("mrr", "Rank of the right note", "test", "mrr", True, "Mean reciprocal rank: 1.0 means the right note was always first."),
    ("miss_rate", "Found nothing", "test", "pct", False, "Questions where recall injected no note at all."),
    ("baseline_hit_at_k", "Plain search, for comparison", "test", "pct", True, "The same questions as one keyword search of the whole prompt, with no ladder."),
    ("false_injection_rate", "Notes for off-topic questions", "all", "pct", False, "Questions the vault cannot answer that still got notes injected."),
    ("p95_ms", "Slowest 5%", "all", "ms", False, "95th percentile recall latency."),
]


def _fmt(v, kind):
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    if kind == "ms":
        return f"{v / 1000:.1f} s" if v >= 1000 else f"{v} ms"
    return f"{v:.2f}"


def render(runs: list[dict], last: Optional[dict], live: Optional[dict], answers: Optional[dict],
           tips: list[dict]) -> str:
    # Only runs on the same question set are comparable: plot the latest run's set.
    # Only runs on the same question set AND the same settings are comparable: a settings change
    # is an experiment, not a trend. Plot the latest run's set and settings.
    current_set, current_cfg = (last or {}).get("set"), (last or {}).get("settings")
    retrieval = [r for r in runs if r.get("kind") == "retrieval" and (not current_set or r.get("set") == current_set)
                 and (not current_cfg or r.get("settings") == current_cfg)]
    series = {}
    for key, label, group, kind, better, help_ in METRICS:
        pts = []
        for r in retrieval:
            g = r["metrics"].get(group) or r["metrics"].get("all") or {}
            if g.get(key) is not None:
                pts.append({"t": r["t"], "v": g[key], "set": r.get("set", "")})
        series[key] = pts
    data = {"metrics": [{"key": k, "label": l, "kind": f, "better": b, "help": h} for k, l, _, f, b, h in METRICS],
            "series": series}
    cur = (last or {}).get("metrics", {})
    def metric_val(key, group):
        return (cur.get(group) or cur.get("all") or {}).get(key)
    tiles = []
    for i, (key, label, group, kind, better, help_) in enumerate(METRICS):
        pts = series[key]
        v = metric_val(key, group)
        delta = ""
        if len(pts) >= 2 and pts[-1]["set"] == pts[-2]["set"]:
            d = pts[-1]["v"] - pts[-2]["v"]
            shown = _fmt(abs(d), kind) if kind != "pct" else f"{abs(d) * 100:.0f} pts"
            if shown not in ("0.00", "0 pts", "0 ms", "0.0 s"):  # a change too small to show is no change
                good = (d > 0) == better
                delta = f'<span class="delta {"good" if good else "bad"}">{"▲" if d > 0 else "▼"} {shown}</span>'
        tiles.append(f'<button class="tile{" on" if i == 0 else ""}" data-key="{key}" title="{html.escape(help_)}">'
                     f'<span class="tl">{html.escape(label)}</span><span class="tv">{_fmt(v, kind)}</span>{delta}</button>')

    rows = (last or {}).get("rows", [])
    failing = [r for r in rows if r["expect"] and r.get("rank") != 1] + [r for r in rows if not r["expect"] and r["status"] == "hit"]
    def case_row(r):
        if not r["expect"]:
            verdict, cls = "noise", "bad"
        elif r.get("rank") is None:
            verdict, cls = ("ranked out" if r.get("in_candidates") else ("no match" if r["status"] != "hit" else "wrong notes")), "bad"
        else:
            verdict, cls = f"rank {r['rank']}", "warn"
        got = "".join(f"<li>{html.escape(h['title'] or h['ref'])}</li>" for h in r.get("injected", [])[:4]) or "<li class='none'>nothing</li>"
        exp = html.escape(", ".join(Path(e).name for e in r["expect"]) or "nothing (off-topic)")
        return (f"<tr><td><span class='pill {cls}'>{verdict}</span></td><td>{html.escape(r['question'])}"
                f"<div class='q'>searched: {html.escape(r.get('query') or '–')}</div></td>"
                f"<td class='mono'>{exp}</td><td><ol>{got}</ol></td><td class='mono'>{r['split']}</td></tr>")
    shown, hidden = failing[:15], failing[15:]
    case_html = "".join(case_row(r) for r in shown)
    if hidden:
        # __CASES__ sits inside the template's <tbody>: close it, add the hidden rows and the button row.
        case_html += ("</tbody><tbody class='more' hidden>" + "".join(case_row(r) for r in hidden) +
                      f"</tbody><tbody><tr><td colspan='5'><button class='showall' type='button'>Show all {len(failing)}</button></td></tr>")
    case_html = case_html or "<tr><td colspan='5' class='empty'>Every question got its note first. Add harder probes: `claude-note eval probes --n 120`.</td></tr>"

    tip_html = "".join(f"<li><b>{html.escape(t['area'])}</b><span>{html.escape(t['text'])}</span></li>" for t in tips) \
        or "<li><b>nothing urgent</b><span>No failure pattern stands out in the last run.</span></li>"

    lv = live or {}
    if lv.get("prompts"):
        live_cards = (f"<div class='kv'><span>Prompts searched ({lv['days']} days)</span><b>{lv['prompts']}</b></div>"
                      f"<div class='kv'><span>Found nothing</span><b>{_fmt(lv.get('miss_rate'), 'pct')}</b></div>"
                      f"<div class='kv'><span>Agent opened an injected note</span><b>{_fmt(lv.get('opened_rate'), 'pct')}</b>"
                      f"<small>{lv.get('turns_checked', 0)} turns checked</small></div>"
                      f"<div class='kv'><span>p95 latency</span><b>{_fmt(lv.get('p95_ms'), 'ms')}</b></div>")
        missed = "".join(f"<tr><td class='mono'>{html.escape(m['query'])}</td><td>{m['times']}</td></tr>" for m in lv.get("top_missed", [])) \
            or "<tr><td colspan='2' class='empty'>No repeated misses.</td></tr>"
        never = "".join(f"<tr><td class='mono'>{html.escape(Path(n['path']).name)}</td><td>{n['shown']}</td></tr>" for n in lv.get("never_opened", [])) \
            or ("<tr><td colspan='2' class='empty'>Every injected note was opened at least once.</td></tr>" if lv.get("turns_checked")
                else "<tr><td colspan='2' class='empty'>No turn could be checked yet: rows logged before this version have no transcript path.</td></tr>")
        live_html = (f"<div class='kvs'>{live_cards}</div><div class='two'>"
                     f"<table><thead><tr><th>Keeps finding nothing</th><th>times</th></tr></thead><tbody>{missed}</tbody></table>"
                     f"<table><thead><tr><th>Injected, never opened</th><th>shown</th></tr></thead><tbody>{never}</tbody></table></div>")
    else:
        live_html = "<p class='empty'>No live data yet. The recall hook logs every prompt; this fills in as you work (install it with <code>claude-note setup</code>).</p>"

    ans = answers or {}
    if ans.get("n"):
        trust = "" if ans.get("judge_trusted", True) else "<p class='warn-t'>The judge failed its control (it found agreement with unrelated notes), so treat these numbers as unreliable.</p>"
        ans_html = (f"<div class='kvs'><div class='kv'><span>Answer follows the team's note, with recall</span><b>{_fmt(ans.get('with_follows'), 'pct')}</b></div>"
                    f"<div class='kv'><span>Without recall</span><b>{_fmt(ans.get('without_follows'), 'pct')}</b></div>"
                    f"<div class='kv'><span>Questions judged</span><b>{ans['n']}</b><small>control: judge said 'neither' for an unrelated note {_fmt(ans.get('control_neither_rate'), 'pct')} of the time</small></div></div>{trust}")
    else:
        ans_html = "<p class='empty'>Not run. <code>claude-note eval answers --n 10</code> asks the same questions with and without recall and judges which answer follows your notes (uses the Claude CLI).</p>"

    meta = ""
    if last:
        m = last["metrics"].get("test") or last["metrics"]["all"]
        meta = (f"Last run {html.escape(last['t'])} · set {html.escape(last.get('set', ''))} · "
                f"{m.get('positives', 0)} questions in the test split · inject {last['settings']['inject']}")

    return TEMPLATE.replace("__META__", meta or "No retrieval run yet: `claude-note eval run`") \
        .replace("__TILES__", "".join(tiles)).replace("__TIPS__", tip_html).replace("__CASES__", case_html) \
        .replace("__NFAIL__", str(len(failing))).replace("__LIVE__", live_html).replace("__ANSWERS__", ans_html) \
        .replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Note evals</title>
<style>
:root{--bg:#fbfaf7;--card:#fff;--ink:#16181d;--mute:#5d6270;--line:#e4e1da;--acc:#e8622a;--good:#2f7a4f;--bad:#b3392a;--warn:#9a6a12;--soft:#f3f1ec}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--card:#1a1c20;--ink:#ecebe7;--mute:#9a9ea8;--line:#2c2f35;--acc:#f07a45;--good:#6cc08f;--bad:#ef7f6f;--warn:#e0b45a;--soft:#22252a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0}h2{font-size:17px;margin:34px 0 12px}
.meta{color:var(--mute);font-size:13px;margin-top:4px}code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px}
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));border-bottom:1px solid var(--line)}
.tile{all:unset;cursor:pointer;padding:14px 16px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:2px;min-width:0}
.tile:last-child{border-right:0}.tile:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
.tile.on{box-shadow:inset 0 -3px 0 var(--acc);background:var(--soft)}.tl{font-size:12px;color:var(--mute)}.tv{font-size:26px;font-weight:650;letter-spacing:-.02em}
.delta{font-size:12px}.delta.good{color:var(--good)}.delta.bad{color:var(--bad)}
.chart{padding:14px 16px 10px}.chart p{margin:0 0 6px;color:var(--mute);font-size:13px}svg{display:block;width:100%;height:190px}
.tips{list-style:none;margin:0;padding:0}.tips li{display:grid;grid-template-columns:150px 1fr;gap:12px;padding:12px 16px;border-top:1px solid var(--line)}.tips li:first-child{border-top:0}.tips b{font-size:13px;color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;font-size:12px;color:var(--mute);font-weight:500;padding:10px 12px;border-bottom:1px solid var(--line)}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}td ol{margin:0;padding-left:18px}td li.none{list-style:none;margin-left:-18px;color:var(--mute)}
.q{color:var(--mute);font-size:12px;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;white-space:nowrap;background:var(--soft)}.pill.bad{color:var(--bad)}.pill.warn{color:var(--warn)}
.empty{color:var(--mute);padding:14px 16px;margin:0}.kvs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}.kv{padding:14px 16px;border-right:1px solid var(--line)}.kv:last-child{border-right:0}
.kv span{display:block;font-size:12px;color:var(--mute)}.kv b{font-size:22px}.kv small{display:block;color:var(--mute);font-size:12px}
.two{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line)}.two table+table{border-left:1px solid var(--line)}.warn-t{color:var(--bad);padding:0 16px 14px;margin:0}
.scroll{overflow-x:auto}.showall{all:unset;cursor:pointer;color:var(--acc);font-weight:600}.showall:focus-visible{outline:2px solid var(--acc)}
@media (max-width:760px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.tile{border-bottom:1px solid var(--line)}.kvs,.two{grid-template-columns:1fr}.kv{border-right:0;border-bottom:1px solid var(--line)}.tips li{grid-template-columns:1fr;gap:2px}.two table+table{border-left:0}}
</style></head><body><main>
<h1>Is recall finding the right notes?</h1><div class="meta">__META__</div>
<div class="panel" style="margin-top:18px"><div class="tiles">__TILES__</div><div class="chart"><p id="help"></p><svg id="trend" role="img" aria-label="Trend across runs"></svg></div></div>
<h2>What to fix next</h2><div class="panel"><ul class="tips">__TIPS__</ul></div>
<h2>Questions that did not get their note first (__NFAIL__)</h2>
<div class="panel scroll"><table><thead><tr><th>Result</th><th>Question</th><th>Expected note</th><th>Injected instead</th><th>Split</th></tr></thead><tbody>__CASES__</tbody></table></div>
<h2>In real use</h2><div class="panel">__LIVE__</div>
<h2>Does it change the answer?</h2><div class="panel">__ANSWERS__</div>
</main>
<script>
const D = __DATA__;
const svg = document.getElementById('trend'), help = document.getElementById('help');
const fmt = (v, k) => v == null ? '–' : k === 'pct' ? Math.round(v * 100) + '%' : k === 'ms' ? (v >= 1000 ? (v / 1000).toFixed(1) + ' s' : v + ' ms') : v.toFixed(2);
function draw(key) {
  const m = D.metrics.find(x => x.key === key), pts = D.series[key] || [];
  help.textContent = m.help + (pts.length < 2 ? ' One run so far: the trend appears after the next run.' : '');
  const W = svg.clientWidth || 900, H = 190, P = 30;
  if (!pts.length) { svg.innerHTML = ''; return; }
  const vs = pts.map(p => p.v), lo = m.kind === 'ms' ? 0 : Math.min(0, ...vs), hi = m.kind === 'pct' ? 1 : Math.max(...vs) * 1.15 || 1;
  const x = i => P + (pts.length === 1 ? (W - 2 * P) / 2 : i * (W - 2 * P) / (pts.length - 1)), y = v => H - P - (v - lo) / (hi - lo) * (H - 2 * P);
  const css = getComputedStyle(document.documentElement), acc = css.getPropertyValue('--acc'), line = css.getPropertyValue('--line'), mute = css.getPropertyValue('--mute');
  let s = `<line x1="${P}" x2="${W - P}" y1="${H - P}" y2="${H - P}" stroke="${line}"/>`;
  s += `<text x="${P}" y="14" fill="${mute}" font-size="11">${fmt(hi, m.kind)}</text>`;
  s += `<polyline fill="none" stroke="${acc}" stroke-width="2.5" points="${pts.map((p, i) => x(i) + ',' + y(p.v)).join(' ')}"/>`;
  pts.forEach((p, i) => { s += `<circle cx="${x(i)}" cy="${y(p.v)}" r="4" fill="${acc}"><title>${p.t} · ${fmt(p.v, m.kind)} · ${p.set}</title></circle>`; });
  s += `<text x="${P}" y="${H - 8}" fill="${mute}" font-size="11">${pts[0].t.slice(0, 10)}</text><text x="${W - P}" y="${H - 8}" fill="${mute}" font-size="11" text-anchor="end">${pts[pts.length - 1].t.slice(0, 10)}</text>`;
  svg.innerHTML = s;
}
document.querySelectorAll('.tile').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t === b)); draw(b.dataset.key); }));
const more = document.querySelector('.more'), btn = document.querySelector('.showall');
if (btn) btn.addEventListener('click', () => { more.hidden = false; btn.closest('tbody').remove(); });
draw(D.metrics[0].key); addEventListener('resize', () => draw(document.querySelector('.tile.on').dataset.key));
</script></body></html>"""
