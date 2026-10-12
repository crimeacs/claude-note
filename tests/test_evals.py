"""Evals: labelled retrieval metrics, live usefulness from transcripts, probes, answer A/B, dashboard."""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import evals, evals_dashboard, recall


def settings(**kw):
    s = recall.Settings()
    s.qmd = "/bin/echo"  # executable; the runner is faked
    s.log = False
    for k, v in kw.items():
        setattr(s, k, v)
    return s


BODY = "A note body long enough to count as a real snippet for recall to inject."


def item(name):
    return {"file": f"qmd://notes/{name}.md", "title": name.replace("-", " "), "snippet": BODY}


def runner_for(table):
    """Fake qmd: a search returns the notes whose every query word is in its keyword set."""
    def run(cmd, timeout):
        q = cmd[cmd.index("search") + 1].lower().split()
        return [item(n) for n, words in table.items() if all(w in words for w in q)]
    return run


TABLE = {
    "never-build-on-a-small-box": {"run", "npm", "box", "build", "memory"},
    "socket-timeout-is-not-a-deadline": {"urlopen", "timeout", "hung", "socket"},
    "sourdough": set(),
}


class MatchTests(unittest.TestCase):
    def test_path_suffix_stem_and_case(self):
        self.assertTrue(evals.matches("/Users/x/notes/sub/Never-Build-On-A-Small-Box.md", ["never-build-on-a-small-box"]))
        self.assertTrue(evals.matches("qmd://notes/sub/never-build-on-a-small-box.md", ["notes/sub/never-build-on-a-small-box.md"]))
        self.assertFalse(evals.matches("qmd://notes/never-build.md", ["never-build-on-a-small-box"]))


class RetrievalTests(unittest.TestCase):
    def run_cases(self, cases, **kw):
        return evals.run_retrieval([evals.Case.from_dict(c) for c in cases], settings(**kw), runner_for(TABLE))

    def test_hit_miss_baseline_and_off_topic(self):
        run = self.run_cases([
            {"id": "a", "question": "Can I just run npm run build on the 4 GB box?", "expect": ["never-build-on-a-small-box"], "split": "test"},
            {"id": "b", "question": "My urlopen call has timeout=10 but it hung for an hour", "expect": ["socket-timeout-is-not-a-deadline"], "split": "test"},
            {"id": "c", "question": "Why do our cron jobs drift by a few minutes every week", "expect": ["cron-drift"], "split": "test"},
            {"id": "d", "question": "How do I center a div with flexbox please", "expect": [], "split": "test"},
        ])
        m = run["metrics"]["test"]
        self.assertAlmostEqual(m["hit_at_k"], 2 / 3)
        self.assertAlmostEqual(m["mrr"], 2 / 3)
        self.assertAlmostEqual(m["miss_rate"], 1 / 3)
        self.assertEqual(m["false_injection_rate"], 0.0)
        # The raw sentence contains words no note has: the plain search finds nothing.
        self.assertEqual(m["baseline_hit_at_k"], 0.0)
        rows = {r["id"]: r for r in run["rows"]}
        self.assertEqual(rows["a"]["rank"], 1)
        self.assertIsNone(rows["c"]["rank"])
        self.assertEqual(run["kind"], "retrieval")

    def test_ranked_out_is_told_apart_from_never_found(self):
        table = {f"note-{i}": {"run", "npm", "box"} for i in range(6)}
        table["never-build-on-a-small-box"] = {"run", "npm", "box"}
        run = evals.run_retrieval([evals.Case.from_dict({"id": "a", "question": "Can I just run npm run build on the box?",
                                                         "expect": ["never-build-on-a-small-box"], "split": "dev"})],
                                  settings(inject=2), runner_for(table))
        row = run["rows"][0]
        self.assertIsNone(row["rank"])
        self.assertTrue(row["in_candidates"])
        self.assertEqual(run["metrics"]["dev"]["ranked_out"], 1.0)
        self.assertIn("ranking", [t["area"] for t in evals.advice({**run, "metrics": {"test": run["metrics"]["dev"]}}, None)])

    def test_a_pushed_copy_with_the_same_title_counts(self):
        def run(cmd, timeout):
            return [{"file": "qmd://foresyn/inbox/me/2026-10-01/copy-of-it.md", "title": "Never run the build on the 4 GB box", "snippet": BODY}]
        case = evals.Case.from_dict({"id": "a", "question": "Can I just run npm run build on the box?",
                                     "expect": ["notes/never-build-on-a-small-box.md"], "split": "dev",
                                     "note_title": "Never run the build on the 4 GB box"})
        self.assertEqual(evals.score_case(case, settings(), run)["rank"], 1)

    def test_split_is_by_note_never_by_row(self):
        a = evals.Case.from_dict({"question": "first question about it", "expect": ["notes/x.md"]})
        b = evals.Case.from_dict({"question": "another way to ask it", "expect": ["notes/x.md"]})
        self.assertEqual(a.split, b.split)

    def test_fixture_is_valid(self):
        cases = evals.load_cases(evals.FIXTURE)
        self.assertGreaterEqual(len(cases), 20)
        self.assertTrue(any(not c.expect for c in cases), "fixture needs off-topic questions")
        by_note = {}
        for c in cases:
            for e in c.expect:
                by_note.setdefault(e, set()).add(c.split)
        self.assertTrue(all(len(s) == 1 for s in by_note.values()), "a note must not appear in both splits")


class SaveTests(unittest.TestCase):
    def test_runs_append_without_rows_and_last_run_keeps_them(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.multiple(
                evals, EVAL_DIR=Path(d), RUNS=Path(d) / "runs.jsonl", LAST_RUN=Path(d) / "last-run.json"):
            run = {"kind": "retrieval", "t": "2026-10-12T00:00:00Z", "metrics": {"all": {}}, "rows": [{"id": "a"}]}
            evals.save_run(run)
            evals.save_run(run)
            self.assertEqual(len(evals.load_runs()), 2)
            self.assertNotIn("rows", evals.load_runs()[0])
            self.assertIn("rows", json.loads((Path(d) / "last-run.json").read_text()))


class ProbeTests(unittest.TestCase):
    def test_build_probes_labels_from_the_source_note(self):
        notes = [{"ref": f"notes/n{i}.md", "path": f"/v/n{i}.md", "type": t, "title": f"Title {i}", "body": BODY * 5}
                 for i, t in enumerate(["gotcha", "decision", "pattern"])]

        seen = {}

        def ask(prompt):
            seen["first"] = prompt.split("[id 0] TITLE: ")[1].split("\n")[0]
            return {"probes": [{"id": "0", "question": "why does the export keep timing out at night"},
                               {"id": "2", "question": "short"},      # too short: dropped
                               {"id": "9", "question": "an id that does not exist in this batch"}]}
        with mock.patch.object(evals, "candidate_notes", return_value=notes):
            probes = evals.build_probes(3, settings=settings(), ask=ask)
        self.assertEqual(len(probes), 1)
        p = probes[0]
        source = next(n for n in notes if n["title"] == seen["first"])
        self.assertEqual(p["expect"], [source["ref"]])
        self.assertEqual(p["split"], evals._split(source["ref"]))
        self.assertEqual(p["source"], "probe")

    def test_first_batch_failure_is_loud(self):
        notes = [{"ref": "notes/a.md", "path": "/v/a.md", "type": "gotcha", "title": "A", "body": BODY * 5}]
        with mock.patch.object(evals, "candidate_notes", return_value=notes):
            with self.assertRaises(RuntimeError):
                evals.build_probes(1, settings=settings(), ask=lambda p: None)

    def test_sampling_is_stratified_by_type(self):
        notes = [{"ref": f"r{i}", "type": "reference"} for i in range(50)] + [{"ref": "g", "type": "gotcha"}]
        picked = evals.sample_notes(notes, 4)
        self.assertIn("gotcha", {n["type"] for n in picked})

    def test_title_overlap(self):
        self.assertEqual(evals.title_overlap("socket timeout hung", "A socket timeout is not a deadline"), 2 / 3)


class LiveTests(unittest.TestCase):
    def test_opened_rate_from_transcript_reads(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            note = "/v/notes/never-build-on-a-small-box.md"
            t0 = time.time() - 600
            iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
            transcript = d / "s.jsonl"
            transcript.write_text("\n".join(json.dumps(r) for r in [
                {"type": "user", "timestamp": iso(t0), "message": {"content": "can I build on the box"}},
                {"type": "assistant", "timestamp": iso(t0 + 3), "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": note}}]}},
                {"type": "user", "timestamp": iso(t0 + 60), "message": {"content": "next question"}},
                {"type": "assistant", "timestamp": iso(t0 + 62), "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/v/notes/other.md"}}]}},
            ]))
            log = d / "recall.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in [
                {"ts": iso(t0)[:19] + "Z", "kind": "prompt", "status": "hit", "ms": 300, "q": "build box",
                 "transcript": str(transcript), "paths": [note]},
                {"ts": iso(t0 + 60)[:19] + "Z", "kind": "prompt", "status": "hit", "ms": 400, "q": "next",
                 "transcript": str(transcript), "paths": ["/v/notes/unread.md"]},
                {"ts": iso(t0 + 90)[:19] + "Z", "kind": "prompt", "status": "miss", "ms": 200, "q": "cron drift"},
                {"ts": iso(t0 + 95)[:19] + "Z", "kind": "prompt", "status": "miss", "ms": 210, "q": "cron drift"},
            ]))
            out = evals.live(30, log)
        self.assertEqual(out["prompts"], 4)
        self.assertEqual(out["turns_checked"], 2)
        self.assertEqual(out["opened_rate"], 0.5)   # the second turn read a note it was not shown
        self.assertEqual(out["never_opened"][0]["path"], "/v/notes/unread.md")
        self.assertEqual(out["top_missed"][0], {"query": "cron drift", "times": 2})

    def test_empty_log(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(evals.live(7, Path(d) / "none.jsonl")["prompts"], 0)


class HookLogTests(unittest.TestCase):
    def test_hook_logs_session_transcript_and_paths(self):
        res = recall.Result(status="hit", query=["build", "box"], hits=[recall.Hit(uri="qmd://notes/a.md", title="A", snippet=BODY, path="/v/a.md")])
        payload = {"prompt": "can I build on the box", "session_id": "s1", "transcript_path": "/t/s1.jsonl"}
        s = settings(log=True)
        with mock.patch.object(recall, "recall", return_value=res), mock.patch.object(recall.Settings, "load", return_value=s), \
                mock.patch.object(recall, "log_event") as log, mock.patch("sys.stdout", new=io.StringIO()):
            recall.hook_main(io.StringIO(json.dumps(payload)))
        kind, _, extra = log.call_args[0]
        self.assertEqual(extra, {"session": "s1", "transcript": "/t/s1.jsonl", "paths": ["/v/a.md"]})


class AnswerTests(unittest.TestCase):
    def test_follow_rates_blind_order_and_control(self):
        probes = [{"id": f"p{i}", "question": f"question number {i} about the build box", "expect": [f"notes/n{i}.md"],
                   "split": "test", "note_type": "decision"} for i in range(4)]
        res = recall.Result(status="hit", hits=[recall.Hit(uri="qmd://notes/n0.md", title="n0", snippet=BODY)])

        def answer(prompt):
            return "Follow the note: build on the laptop." if "claude-note recall" in prompt else "Sure, you can build there."

        def ask(prompt):
            if "Follow the note" not in prompt:
                return {"follows": "neither"}
            x_is_with = prompt.index("Follow the note") < prompt.index("Sure, you can")
            note_is_real = "team rule" in prompt
            return {"follows": ("X" if x_is_with else "Y") if note_is_real else "neither"}
        with mock.patch.object(evals, "_note_text", side_effect=lambda ref, s: f"team rule for {ref}"), \
                mock.patch.object(recall, "recall", return_value=res), mock.patch.object(recall, "format_context", return_value="claude-note recall: n0"):
            out = evals.run_answers(probes, n=4, settings=settings(), ask=ask, answer=answer)
        self.assertEqual(out["n"], 4)
        self.assertEqual(out["with_follows"], 1.0)
        self.assertEqual(out["without_follows"], 0.0)


class DashboardTests(unittest.TestCase):
    def test_renders_self_contained(self):
        run = evals.run_retrieval([evals.Case.from_dict({"id": "a", "question": "Can I just run npm run build on the 4 GB box?",
                                                         "expect": ["never-build-on-a-small-box"], "split": "test"}),
                                   evals.Case.from_dict({"id": "c", "question": "Why do our cron jobs drift every single week",
                                                         "expect": ["cron-drift"], "split": "test"})],
                                  settings(), runner_for(TABLE))
        page = evals_dashboard.render([{k: v for k, v in run.items() if k != "rows"}], run, {"prompts": 0, "days": 30}, None,
                                      evals.advice(run, None))
        self.assertIn("Is recall finding the right notes?", page)
        self.assertIn("Why do our cron jobs drift", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", page)


if __name__ == "__main__":
    unittest.main()
