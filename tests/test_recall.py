"""Recall before action: query construction, pooling, presentation, sentinel, remember, setup."""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import recall, remember, sentinel, setup_recall


def settings(**kw):
    s = recall.Settings()
    s.qmd = "/bin/echo"
    s.engine = "cli"  # never reach a real daemon from a unit test  # executable; runner is mocked
    s.log = False
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def item(uri, title, snippet="A note body long enough to count as a real snippet for recall."):
    return {"file": uri, "title": title, "snippet": snippet, "score": 0.5}


class QueryTests(unittest.TestCase):
    def test_drops_stopwords_paths_dates_and_imperatives(self):
        terms = recall.content_terms(
            "Please summarise how we organise obsidian note types in /Users/me/notes on 2026-09-20")
        self.assertEqual(terms, ["organise", "obsidian", "note", "types"])

    def test_splits_hyphen_snake_and_dotted_compounds(self):
        self.assertEqual(recall.content_terms("task-backed source_unchanged config.toml"),
                         ["task", "backed", "source", "unchanged", "config", "toml"])

    def test_strips_wrapper_markup_but_keeps_text(self):
        terms = recall.content_terms('<pasted_content id="x">Typeform progressive form</pasted_content>')
        self.assertEqual(terms, ["Typeform", "progressive", "form"])

    def test_ladder_keeps_discriminating_terms_in_prompt_order(self):
        terms = ["make", "Typeform", "experience", "best", "practices", "onboarding"]
        ladder = recall.query_ladder(terms, first_word="make")
        self.assertEqual(ladder[0], terms)
        self.assertIn("Typeform", ladder[-1])
        self.assertEqual(len(ladder[-1]), 2)

    def test_first_word_capital_is_grammar_not_a_proper_noun(self):
        ladder = recall.query_ladder(["What", "retry", "idempotency"], first_word="What")
        self.assertNotIn("What", ladder[-1])


class RecallTests(unittest.TestCase):
    def test_pools_every_rung_and_ranks_by_votes(self):
        calls = []

        def runner(cmd, timeout):
            calls.append(cmd)
            q = cmd[cmd.index("search") + 1]
            out = [item("qmd://notes/wide.md", "Wide")]
            if len(q.split()) <= 3:
                out = [item("qmd://notes/narrow.md", "Narrow")] + out
            return out

        r = recall.recall("retry idempotency key duplicate payment charge", settings(), runner)
        self.assertEqual(r.status, "hit")
        self.assertEqual(r.hits[0].title, "Wide")  # every rung agreed on it
        self.assertGreater(len(calls), 1)

    def test_miss_is_announced_with_the_query(self):
        r = recall.recall("zeppelin hangar lighting schedule", settings(), lambda c, t: [])
        self.assertEqual(r.status, "miss")
        text = recall.format_context(r)
        self.assertIn("found nothing", text)
        self.assertIn("zeppelin", text)

    def test_unavailable_qmd_fails_open(self):
        r = recall.recall("zeppelin hangar lighting schedule", settings(), lambda c, t: None)
        self.assertEqual(r.status, "unavailable")
        self.assertIn("found nothing", recall.format_context(r))

    def test_skips_sessions_inbox_and_bare_frontmatter(self):
        got = [item("qmd://notes/sessions/s1.md", "Session"),
               item("qmd://notes/claude-note-inbox.md", "Inbox"),
               item("qmd://notes/fm.md", "FM", "type: gotcha tags: [x]"),
               item("qmd://notes/real.md", "Real")]
        r = recall.recall("retry idempotency duplicate payment", settings(), lambda c, t: got)
        self.assertEqual([h.title for h in r.hits], ["Real"])

    def test_skips_automated_events_short_prompts_and_slash_commands(self):
        for prompt in ("<task-notification>done</task-notification> bhww6b9lv",
                       "ok thanks", "/compact keep going please now"):
            self.assertEqual(recall.recall(prompt, settings(), lambda c, t: []).status, "skipped")

    def test_memory_collection_gets_reserved_slots(self):
        def runner(cmd, timeout):
            if "-c" in cmd and cmd[cmd.index("-c") + 1] == "agentmem":
                return [item("qmd://agentmem/p/memory/lesson.md", "Lesson")]
            return [item(f"qmd://notes/n{i}.md", f"N{i}") for i in range(6)]

        r = recall.recall("retry idempotency duplicate payment",
                          settings(memory_collections=["agentmem"], inject=3), runner)
        self.assertEqual(r.hits[0].title, "Lesson")
        self.assertTrue(r.hits[0].memory)
        self.assertEqual(len(r.hits), 3)

    def test_index_flag_and_collections_are_passed(self):
        seen = []
        recall.recall("retry idempotency duplicate payment",
                      settings(index="demo", collections=["notes"]),
                      lambda c, t: seen.append(c) or [])
        self.assertEqual(seen[0][1:3], ["--index", "demo"])
        self.assertEqual(seen[0][-2:], ["-c", "notes"])

    def test_rerank_command_filters_and_failure_keeps_order(self):
        got = [item("qmd://notes/a.md", "A"), item("qmd://notes/b.md", "B")]
        keep_b = 'python3 -c \'print("{\\"keep\\": [\\"c1\\"]}")\''
        r = recall.recall("retry idempotency duplicate payment", settings(rerank_command=keep_b),
                          lambda c, t: got)
        self.assertEqual([h.title for h in r.hits], ["B"])
        r = recall.recall("retry idempotency duplicate payment", settings(rerank_command="exit 3"),
                          lambda c, t: got)
        self.assertEqual([h.title for h in r.hits], ["A", "B"])

    def test_freshest_block_shows_newest_update(self):
        text = ("intro\n<!-- claude-note:synth-2026-01-01:start -->\n## Old\nold text here\n"
                "<!-- claude-note:synth-2026-01-01:end -->\n"
                "<!-- claude-note:synth-2026-09-30:start -->\n## New\nthe current answer\n"
                "<!-- claude-note:synth-2026-09-30:end -->\n")
        label, body = recall.freshest_block(text, 200)
        self.assertIn("2026-09-30", label)
        self.assertEqual(body, "the current answer")

    def test_frontmatter_only_snippet_falls_back_to_note_body(self):
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "build.md"
            note.write_text("---\ntitle: Never build on the small box\ntype: decision\n---\n"
                            "# Never build on the small box\n\nThe build runs out of memory and takes the app down.\n")
            got = [item("qmd://notes/build.md", "Never build", "title: Never build on the small box")]
            with mock.patch.object(recall, "local_path", return_value=note):
                r = recall.recall("production build small box memory", settings(), lambda c, t: got)
        self.assertEqual(r.status, "hit")
        self.assertEqual(r.hits[0].type, "decision")
        self.assertIn("runs out of memory", r.hits[0].snippet)

    def test_hook_emits_additional_context(self):
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "zeppelin hangar lighting schedule"}
        out = io.StringIO()
        with mock.patch.object(recall.Settings, "load", return_value=settings()), \
                mock.patch.object(recall, "_run_qmd", return_value=[]), \
                mock.patch("sys.stdout", out):
            recall.hook_main(io.StringIO(json.dumps(payload)))
        data = json.loads(out.getvalue())
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("found nothing", data["hookSpecificOutput"]["additionalContext"])


class SentinelTests(unittest.TestCase):
    def test_reads_output_never_the_command(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "grep invalid_grant hook.sh"},
                   "tool_response": {"stdout": "12: invalid_grant|401 Unauthorized", "stderr": ""}}
        self.assertEqual(sentinel.failure_text(payload), "")

    def test_failure_payload_and_rules(self):
        payload = {"tool_name": "Bash", "error": "Exit code 1\ncurl: (22) The requested URL returned error: 401"}
        found = sentinel.analyse(payload, settings(), lambda c, t: [])
        self.assertTrue(found["advice"])
        self.assertIn("401", found["line"])

    def test_disk_full_rule(self):
        found = sentinel.analyse({"tool_name": "Bash", "error": "ENOSPC: no space left on device, mkdir '/tmp/x'"},
                                 settings(), lambda c, t: [])
        self.assertTrue(any("disk is full" in a for a in found["advice"]))

    def test_prefers_gotchas_and_strips_exception_names(self):
        queries = []

        def runner(cmd, timeout):
            queries.append(cmd[cmd.index("search") + 1])
            return [item("qmd://notes/fix.md", "Fix")]

        with mock.patch.object(recall, "local_path", return_value=None):
            found = sentinel.analyse({"tool_name": "Bash",
                                      "error": "AssertionError: customer charged twice after payment retry"},
                                     settings(), runner)
        self.assertNotIn("AssertionError", queries[0])
        self.assertEqual(found["hits"][0].title, "Fix")

    def test_error_identifiers_are_searched_singly(self):
        queries = []

        def runner(cmd, timeout):
            q = cmd[cmd.index("search") + 1]
            queries.append(q)
            return [item("qmd://notes/oauth.md", "OAuth")] if q == "insufficientPermissions" else []

        with mock.patch.object(recall, "local_path", return_value=None):
            found = sentinel.analyse({"tool_name": "Bash", "error":
                "googleapiclient.errors.HttpError: <HttpError 403 when requesting https://x.io/a/b "
                "returned \"insufficient scopes\". Details: [{'reason': 'insufficientPermissions'}]"},
                settings(), runner)
        self.assertIn("insufficientPermissions", queries)
        self.assertFalse(any("googleapiclient" in q or "https" in q for q in queries))
        self.assertEqual(found["hits"][0].title, "OAuth")

    def test_non_bash_tools_are_ignored(self):
        self.assertEqual(sentinel.failure_text({"tool_name": "Read", "error": "File not found"}), "")

    def test_once_per_session(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sentinel, "_seen_path", return_value=Path(d) / "seen.json"):
            self.assertTrue(sentinel.first_time("s1", "abc"))
            self.assertFalse(sentinel.first_time("s1", "abc"))
            self.assertTrue(sentinel.first_time("s2", "abc"))


class RememberTests(unittest.TestCase):
    def test_creates_typed_note_then_appends_dated_block(self):
        from claude_note import provenance
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_NOTE_AUTHOR": "a@b.c"}), \
                mock.patch.object(provenance, "_author_cache", None):
            vault = Path(d)
            path, created = remember.write_note(
                vault, "Retries double-charge: mint the idempotency key once.", note_type="gotcha",
                tags=["payments"], why="Lost responses are retried.", assistant="codex",
                now=datetime(2026, 10, 10, 12, 0, 0))
            self.assertTrue(created)
            text = path.read_text()
            self.assertIn("type: gotcha", text)
            self.assertIn("assistant: codex", text)
            self.assertIn("author: a@b.c", text)
            self.assertIn("**Why:** Lost responses are retried.", text)
            path2, created2 = remember.write_note(
                vault, "Also enforce UNIQUE on the server.", title=remember.derive_title(
                    "Retries double-charge: mint the idempotency key once."),
                now=datetime(2026, 10, 11, 9, 0, 0))
            self.assertEqual(path, path2)
            self.assertFalse(created2)
            text = path.read_text()
            self.assertIn("## Update 2026-10-11", text)
            self.assertIn("UNIQUE", text)
            self.assertEqual(text.count("type: gotcha"), 1)

    def test_rejects_traversal_hidden_folders_and_bad_types(self):
        with tempfile.TemporaryDirectory() as d:
            for kw in ({"folder": "../out"}, {"folder": ".claude-note"}, {"note_type": "session"}):
                with self.assertRaises(ValueError):
                    remember.write_note(Path(d), "text", **kw)


class SetupTests(unittest.TestCase):
    def test_install_hooks_is_idempotent_and_keeps_other_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({"model": "x", "hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "claude-note enqueue"}]}]}}))
            added = setup_recall.install_hooks(path, "/bin/claude-note")
            self.assertEqual(len(added), 3)
            self.assertEqual(setup_recall.install_hooks(path, "/bin/claude-note"), [])
            data = json.loads(path.read_text())
            self.assertEqual(data["model"], "x")
            self.assertEqual(data["hooks"]["PostToolUseFailure"][0]["matcher"], "Bash")
            self.assertEqual(setup_recall.hooks_status(path),
                             {"recall": True, "sentinel": True, "capture": True})

    def test_codex_gets_recall_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            added = setup_recall.install_hooks(path, "/bin/claude-note", codex=True)
            self.assertEqual(added, ["UserPromptSubmit: hook recall"])

    def test_rule_block_is_added_once_and_updated_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CLAUDE.md"
            path.write_text("# Mine\n\nkeep this\n")
            self.assertEqual(setup_recall.install_rule(path), "added")
            self.assertEqual(setup_recall.install_rule(path), "up to date")
            text = path.read_text()
            self.assertEqual(text.count(setup_recall.RULE_START), 1)
            self.assertIn("keep this", text)


if __name__ == "__main__":
    unittest.main()


class DaemonEngineTests(unittest.TestCase):
    """QMD 2.x daemon: one request with the ladder as keyword searches plus the question by meaning."""

    def test_one_request_vector_first_and_fused_order_kept(self):
        sent = {}

        def fake(settings_, body, deadline):
            sent.update(body)
            return [item("qmd://notes/b.md", "B"), item("qmd://notes/a.md", "A")]
        s = settings(engine="daemon")
        with mock.patch.object(recall, "daemon_query", side_effect=fake):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", s, runner=lambda c, t: self.fail("no CLI call"))
        self.assertEqual(res.engine, "daemon")
        self.assertEqual(sent["searches"][0]["type"], "vec")
        self.assertTrue(all(x["type"] == "lex" for x in sent["searches"][1:]))
        self.assertFalse(sent["rerank"])
        self.assertEqual([h.uri for h in res.hits], ["qmd://notes/b.md", "qmd://notes/a.md"])

    def test_auto_falls_back_to_the_cli_ladder(self):
        calls = []
        s = settings(engine="auto")
        with mock.patch.object(recall, "daemon_query", return_value=None):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", s,
                                runner=lambda c, t: calls.append(c) or [item("qmd://notes/a.md", "A")])
        self.assertTrue(calls)
        self.assertEqual(res.engine, "cli")
        self.assertEqual(res.status, "hit")

    def test_daemon_only_reports_unavailable(self):
        with mock.patch.object(recall, "daemon_query", return_value=None):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", settings(engine="daemon"))
        self.assertEqual(res.status, "unavailable")
