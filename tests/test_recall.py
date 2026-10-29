"""Recall before action: query construction, pooling, presentation, sentinel, remember, setup."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
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

    def test_complementary_pairs_drop_one_poisoned_term_without_guessing(self):
        ladder = [["test", "knowdge", "design", "everyday"],
                  ["knowdge", "design", "everyday"], ["knowdge", "everyday"]]
        self.assertEqual(recall.complementary_pairs(ladder),
                         [["design", "everyday"], ["knowdge", "design"]])
        self.assertEqual(recall.complementary_pairs([["Google", "Drive", "access"], ["Google", "access"]]),
                         [["Drive", "access"], ["Google", "Drive"]])
        self.assertEqual(recall.complementary_pairs([["codex", "hooks"]]), [])


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

    def test_subject_free_followup_never_searches_generic_action_notes(self):
        for prompt in ("fix it", "Please repair this!", "check that please", "get it to work! fix it",
                       "get this to work", "please fix it and check that"):
            with self.subTest(prompt=prompt), mock.patch.object(recall, "server_post") as post:
                runner = mock.Mock(return_value=[item("qmd://notes/random-fix.md", "Old incident fix")])
                result = recall.recall(prompt, settings(engine="auto"), runner)
                self.assertEqual(result.status, "skipped")
                self.assertIn("no retrieval subject", result.reason)
                runner.assert_not_called()
                post.assert_not_called()
        for prompt in ("codex hooks", "INSUFFICIENTPERMISSIONS", "fix OAuth retry", "get OAuth to work",
                       "fix it! Citizens bank"):
            with self.subTest(prompt=prompt):
                runner = mock.Mock(return_value=[])
                result = recall.recall(prompt, settings(), runner)
                self.assertEqual(result.status, "miss")
                runner.assert_called()

    def test_subject_free_acknowledgements_skip_both_engines_and_context_injection(self):
        for prompt in ("yes", "NO.", "okay", "Thanks!", "thank you", "yes please", "no, thanks",
                       "Okay, thank you.", "sure", "yep!", "nope"):
            with self.subTest(prompt=prompt), mock.patch.object(recall, "server_post") as post:
                runner = mock.Mock(return_value=[item("qmd://notes/yes.md", "Unrelated yes note")])
                result = recall.recall(prompt, settings(engine="auto"), runner)
                self.assertEqual(result.status, "skipped")
                self.assertIn("no retrieval subject", result.reason)
                self.assertEqual(recall.format_context(result), "")
                runner.assert_not_called()
                post.assert_not_called()

    def test_acknowledgement_words_do_not_hide_a_short_subject_or_explicit_query(self):
        for prompt in ("yes hooks", "no authentication", "okay OAuth", "THANKYOU_ERROR", "codex hooks"):
            with self.subTest(prompt=prompt):
                runner = mock.Mock(return_value=[])
                result = recall.recall(prompt, settings(), runner)
                self.assertEqual(result.status, "miss")
                runner.assert_called()
        runner = mock.Mock(return_value=[])
        result = recall.recall("yes", settings(), runner, extra_rungs=[["codex", "hooks"]])
        self.assertEqual(result.status, "miss")
        runner.assert_called()

    def test_actual_hook_entrypoint_stays_quiet_for_acknowledgements(self):
        for prompt in ("yes", "no", "okay", "thanks"):
            output = io.StringIO()
            payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
            with self.subTest(prompt=prompt), \
                    mock.patch.object(recall.Settings, "load", return_value=settings(engine="auto")), \
                    mock.patch.object(recall, "server_post") as post, \
                    mock.patch.object(recall, "memory_md_warning", return_value=""), \
                    mock.patch("sys.stdout", output):
                self.assertEqual(recall.hook_main(io.StringIO(json.dumps(payload))), 0)
            self.assertEqual(output.getvalue(), "")
            post.assert_not_called()

    def test_cli_search_preserves_time_for_actual_source_verification(self):
        # Every rung is slow, but returns a real source. Searching until the
        # whole deadline would erase every result during source preparation.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth.md"
            path.write_text("---\ntype: gotcha\n---\nOAuth retry requires removing the stale token before requesting permissions.\n")
            calls = []
            def runner(cmd, timeout):
                calls.append(timeout)
                time.sleep(min(.09, timeout * .7))
                return [item("qmd://notes/oauth.md", "OAuth retry")]
            def resolve(*_args, **_kwargs):
                time.sleep(.035)
                return path
            with mock.patch.object(recall, "local_path", side_effect=resolve):
                started = time.monotonic()
                result = recall.recall("OAuth retry stale token permission scope", settings(
                    budget=.35, memory_collections=["agentmem"]), runner)
            self.assertLess(time.monotonic() - started, .45)
            self.assertEqual(result.status, "hit")
            self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
            self.assertEqual(result.hits[0].anchor["start_line"], 4)
            self.assertGreater(len(calls), 1)

    def test_incomplete_cli_ladder_is_unavailable_instead_of_a_false_miss(self):
        release = threading.Event()
        calls = []
        def runner(cmd, timeout):
            calls.append(cmd)
            if len(calls) == 1:
                return []
            release.wait(1)  # deliberately ignores the adapter's timeout
            return []
        try:
            started = time.monotonic()
            result = recall.recall("OAuth payment retry permission stale token", settings(budget=.12), runner)
            self.assertLess(time.monotonic() - started, .25)
            self.assertEqual(result.status, "unavailable")
            self.assertTrue(result.coverage["partial"])
            self.assertTrue(result.deadline_exhausted)
            self.assertIn("coverage incomplete", result.reason)
        finally:
            release.set()

    def test_source_resolution_timeout_is_unavailable_instead_of_no_eligible_passages(self):
        release = threading.Event()
        try:
            with mock.patch.object(recall, "local_path", side_effect=lambda *_a, **_k: release.wait(1)):
                result = recall.recall("OAuth retry", settings(budget=.05),
                                       lambda *_: [item("qmd://notes/oauth.md", "OAuth")])
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.reason, "source verification deadline exhausted")
            self.assertEqual(result.coverage["local_resolution"], "partial_deadline")
        finally:
            release.set()

    def test_incomplete_search_cannot_turn_an_ineligible_pool_into_a_miss(self):
        calls = []
        def runner(*_args):
            calls.append(True)
            return [item("qmd://notes/fm.md", "Metadata", "type: gotcha")] if len(calls) == 1 else None
        with mock.patch.object(recall, "local_path", return_value=None):
            result = recall.recall("OAuth payment retry stale token", settings(), runner)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("coverage incomplete", result.reason)

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
        keep_b = 'python3 -c \'print("{\\"keep\\": [\\"c1\\"]}")\''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            got = [item("qmd://notes/a.md", "A", "Retry idempotency prevents a duplicate payment: synthetic lesson A."),
                   item("qmd://notes/b.md", "B", "Retry idempotency prevents a duplicate payment: synthetic lesson B.")]
            for row in got:
                root.joinpath(row["file"].rsplit("/", 1)[-1]).write_text(row["snippet"])
            rules = {"rules": [{"source": str(root / "*.md"), "audiences": ["host", "model"]}]}
            with mock.patch("claude_note.egress.policy", return_value=rules), \
                    mock.patch.object(recall, "local_path", side_effect=lambda uri, *args, **kwargs: root / uri.rsplit("/", 1)[-1]):
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


class ServerEngineTests(unittest.TestCase):
    """The warm recall server: keyword rungs + meaning, fused here, gated, reranked."""

    def lists(self):
        body = "A note body long enough to count as a real snippet for recall to inject."
        lex = [[{"file": "qmd://notes/a.md", "title": "A", "score": 0.9, "snippet": body}, {"file": "qmd://notes/b.md", "title": "B", "score": 0.5, "snippet": body}]]
        vec = [{"file": "qmd://notes/b.md", "title": "B", "score": 0.62, "snippet": body}, {"file": "qmd://foresyn/inbox/x/b-copy.md", "title": "B", "score": 0.6, "snippet": body},
               {"file": "qmd://notes/c.md", "title": "C", "score": 0.55, "snippet": body}]
        # Mirrors require explicit source/version equality; a matching title
        # alone is not a source identity.
        for row in lex[0] + vec:
            if row["title"] == "B":
                row.update(source_id="source:b", revision="b" * 64)
        return lex, vec

    def test_fusion_weights_meaning_and_merges_pushed_copies(self):
        lex, vec = self.lists()
        fused, best = recall.fuse(lex, vec, settings(vector_weight=3.0))
        self.assertEqual([h["file"] for h in fused], ["qmd://notes/b.md", "qmd://notes/c.md", "qmd://notes/a.md"])
        self.assertAlmostEqual(best, 0.62)

    def test_gate_injects_nothing_for_far_questions(self):
        lex, vec = self.lists()
        vec = [{**h, "score": 0.2} for h in vec]
        with mock.patch.object(recall, "server_post", return_value={"lex": lex, "vec": vec}):
            res = recall.recall("How do I keep basil alive on a windowsill in winter?", settings(engine="server", rerank=False))
        self.assertEqual(res.status, "miss")
        self.assertIn("no admissible lexical evidence or close meaning", res.reason)

    def test_rerank_orders_the_injected_notes(self):
        lex, vec = self.lists()
        for row in lex[0]:
            row["lexical_evidence"] = True

        def post(s_, path, body, deadline):
            if path == "/search":
                return {"lex": lex, "vec": vec}
            scores = {"qmd://notes/a.md": 0.9, "qmd://notes/b.md": 0.1, "qmd://notes/c.md": 0.5}
            return {"passage_scores": {p["passage_id"]: scores[p["file"]] for p in body["passages"]},
                    "scored_passage_ids": [p["passage_id"] for p in body["passages"]],
                    "rerank_status": "scored", "coverage": "exact_passage", "model": "synthetic:ranker-v1",
                    "manifest": {"rerank_ready": True, "models": {"rerank": {"id": "synthetic:ranker-v1", "ready": True}}}}
        with mock.patch.object(recall, "server_post", side_effect=post), \
                mock.patch.object(recall, "local_path", return_value=None):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", settings(engine="server"))
        self.assertTrue(res.reranked)
        self.assertEqual(res.engine, "server")
        self.assertEqual(res.candidates[:3], ["qmd://notes/a.md", "qmd://notes/c.md", "qmd://notes/b.md"])

    def test_auto_falls_back_to_the_cli_ladder(self):
        calls = []
        with mock.patch.object(recall, "server_post", return_value=None):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", settings(engine="auto"),
                                runner=lambda c, t: calls.append(c) or [item("qmd://notes/a.md", "A")])
        self.assertTrue(calls)
        self.assertEqual(res.engine, "cli")

    def test_server_down_typo_prompt_recovers_original_subject_via_cli(self):
        prompt = "test it, what do we have in knowdge on design of everyday things"
        body = "The Design of Everyday Things explains how visible affordances and feedback make actions understandable."
        queries = []
        document_terms = {word.casefold() for word in recall.content_terms(body)}
        def runner(cmd, timeout):
            query = cmd[cmd.index("search") + 1]
            queries.append(query)
            return [item("qmd://notes/book.md", "The Design of Everyday Things", body)] \
                if set(query.casefold().split()) <= document_terms else []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.md"
            path.write_text(body)
            with mock.patch.object(recall, "server_post", return_value={"error": "transport_error"}), \
                    mock.patch.object(recall, "local_path", return_value=path):
                result = recall.recall(prompt, settings(engine="auto"), runner)
        self.assertEqual(result.status, "hit")
        self.assertEqual(result.attempted_engines, ["server", "cli"])
        self.assertEqual(queries[:2], ["knowdge everyday", "design everyday"])
        self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
        self.assertEqual(result.reason, "")
        self.assertEqual(result.coverage["search"], "transport_error")
        self.assertEqual(result.coverage["semantic"], "unavailable_cli")
        self.assertTrue(result.coverage["partial"])
        self.assertIn("Coverage is partial", recall.format_context(result))

    def test_timed_out_server_leaves_two_normal_cli_attempts_and_source_time(self):
        release = threading.Event()
        prompt = "test knowdge design everyday things"
        body = "The Design of Everyday Things explains how visible affordances and feedback make actions understandable."
        queries, completed = [], []
        document_terms = {word.casefold() for word in recall.content_terms(body)}
        def runner(cmd, timeout):
            query = cmd[cmd.index("search") + 1]
            queries.append(query)
            time.sleep(.09)
            completed.append(query)
            return [item("qmd://notes/book.md", "The Design of Everyday Things", body)] \
                if set(query.casefold().split()) <= document_terms else []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "book.md"
                path.write_text(body)
                with mock.patch.object(recall, "server_post", side_effect=lambda *_: release.wait(2)), \
                        mock.patch.object(recall, "local_path", return_value=path):
                    started = time.monotonic()
                    result = recall.recall(prompt, settings(engine="auto", timeout=6, budget=.8), runner)
                self.assertLess(time.monotonic() - started, .95)
            self.assertEqual(result.status, "hit")
            self.assertEqual(queries[:2], ["knowdge everyday", "design everyday"])
            self.assertIn("design everyday", completed)
            self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
            self.assertTrue(result.coverage["partial"])
            self.assertEqual(result.coverage["semantic"], "unavailable_cli")
        finally:
            release.set()
            time.sleep(.1)  # release the final deliberately late CLI fixture

    def test_fallback_protects_original_narrow_query_when_complementary_pairs_miss(self):
        release = threading.Event()
        body = "Knowledge of everyday interactions makes familiar controls and feedback easier to understand."
        document_terms = {word.casefold() for word in recall.content_terms(body)}
        queries = []
        def runner(cmd, timeout):
            query = cmd[cmd.index("search") + 1]
            queries.append(query)
            time.sleep(.09)
            return [item("qmd://notes/knowledge.md", "Everyday knowledge", body)] \
                if set(query.casefold().split()) <= document_terms else []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "knowledge.md"
                path.write_text(body)
                with mock.patch.object(recall, "server_post", side_effect=lambda *_: release.wait(2)), \
                        mock.patch.object(recall, "local_path", return_value=path):
                    started = time.monotonic()
                    result = recall.recall("knowledge desgn everyday", settings(
                        engine="auto", timeout=6, budget=.8), runner)
                self.assertLess(time.monotonic() - started, .95)
            self.assertEqual(queries[:3], ["knowledge everyday", "desgn everyday", "knowledge desgn"])
            self.assertEqual(result.status, "hit")
            self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
            self.assertTrue(result.coverage["partial"])
        finally:
            release.set()
            time.sleep(.1)

    def test_fallback_scheduling_preserves_original_rung_ranking(self):
        calls = []
        prompt = "OAuth payment retry permission stale token"
        narrow_query = " ".join(recall.query_ladder(recall.content_terms(prompt), "OAuth")[-1])
        def runner(cmd, timeout):
            query = cmd[cmd.index("search") + 1]
            calls.append(query)
            if query == narrow_query:
                return [item("qmd://notes/narrow.md", "Narrow")]
            if len(query.split()) == 6:
                return [item("qmd://notes/wide.md", "Wide")]
            return []
        with mock.patch.object(recall, "server_post", return_value=None), \
                mock.patch.object(recall, "local_path", return_value=None):
            result = recall.recall(prompt, settings(engine="auto"), runner)
        self.assertEqual(len(calls[0].split()), 2)
        self.assertEqual(result.hits[0].title, "Wide")

    def test_server_only_reports_unavailable(self):
        with mock.patch.object(recall, "server_post", return_value=None):
            res = recall.recall("Can I just run npm run build on the 4 GB box?", settings(engine="server"))
        self.assertEqual(res.status, "unavailable")


class ServerTransportTests(unittest.TestCase):
    def response(self, body, status=200):
        response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.status = status
        return response

    def test_structured_http_failure_preserves_identity_and_denies_score_shaped_body(self):
        response = self.response({"error": "queue_full", "vec": [{"file": "private"}],
                                  "passage_scores": {"p": .9}, "manifest": {"ok": True}}, 503)
        error = urllib.error.HTTPError("http://private.invalid/secret", 503, "private text", {}, response)
        with mock.patch("urllib.request.urlopen", side_effect=error) as open_url:
            got = recall.server_post(settings(), "/rerank", {"question": "private question"}, 1)
        self.assertEqual(set(got), {"error", "manifest", "client_transport"})
        receipt = got["client_transport"]
        self.assertEqual((receipt["route"], receipt["http_status"], receipt["outcome"]),
                         ("/rerank", 503, "http_error"))
        sent = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(receipt["request_id"], sent["request_id"])
        self.assertNotIn("private", json.dumps(receipt))

    def test_malformed_and_oversized_responses_are_distinct_failures(self):
        for body, outcome in ((b"not JSON", "invalid_response_json"),
                              (b"{\"a\":\"\xff\"}", "invalid_response_json"),
                              (b"[]", "invalid_response_object"),
                              (b"x" * 4_000_001, "response_too_large")):
            with self.subTest(outcome=outcome), mock.patch("urllib.request.urlopen", return_value=self.response(body)):
                got = recall.server_post(settings(), "/search", {}, 1)
            self.assertEqual(got["error"], outcome)
            self.assertEqual(got["client_transport"]["http_status"], 200)

    def test_transport_error_and_socket_timeout_do_not_persist_exception_text(self):
        for error, outcome in ((urllib.error.URLError("secret hostname"), "transport_error"),
                               (urllib.error.URLError(TimeoutError("secret hostname")), "socket_timeout")):
            with self.subTest(outcome=outcome), mock.patch("urllib.request.urlopen", side_effect=error):
                got = recall.server_post(settings(), "/search", {}, 1)
            self.assertEqual(got["error"], outcome)
            self.assertNotIn("secret", json.dumps(got))

    def test_unknown_or_empty_server_errors_never_admit_payload_or_persist_error_text(self):
        for error in ("private URL and secret question", "", False, {"private": "text"}):
            with self.subTest(error=error), mock.patch("urllib.request.urlopen", return_value=self.response({
                    "error": error, "vec": [{"file": "secret"}], "passage_scores": {"p": .9}})):
                got = recall.server_post(settings(), "/search", {"request_id": "private-caller-identifier"}, 1)
            self.assertEqual(got["error"], "server_error")
            self.assertEqual(got["client_transport"]["outcome"], "server_error")
            self.assertNotIn("private", json.dumps(got))
            self.assertNotIn("secret", json.dumps(got))

    def test_absolute_http_deadline_is_retained_and_late_read_cannot_mutate_receipt(self):
        release = threading.Event()
        response = self.response({"lex": []})
        original_read = response.read
        response.read = lambda n: (release.wait(1), original_read(n))[1]
        try:
            with mock.patch("urllib.request.urlopen", return_value=response):
                started = time.monotonic()
                got = recall.server_post(settings(), "/search", {}, .03)
            self.assertLess(time.monotonic() - started, .3)
            self.assertEqual(got["error"], "client_deadline")
            frozen = json.dumps(got, sort_keys=True)
            release.set()
            time.sleep(.03)
            self.assertEqual(json.dumps(got, sort_keys=True), frozen)
        finally:
            release.set()

    def test_read_slot_exhaustion_is_not_reported_as_a_socket_or_elapsed_deadline(self):
        with mock.patch.object(recall, "_READ_SLOTS", threading.BoundedSemaphore(0)):
            got = recall.server_post(settings(), "/search", {}, 1)
        self.assertEqual(got["error"], "client_read_capacity")

    def test_search_subdeadline_retains_remaining_recall_budget_for_auto_fallback(self):
        for engine in ("server", "auto"):
            release = threading.Event()
            response = self.response({"lex": []})
            original_read = response.read
            response.read = lambda n: (release.wait(1), original_read(n))[1]
            calls = []
            try:
                with self.subTest(engine=engine), mock.patch("urllib.request.urlopen", return_value=response):
                    result = recall.recall("payment retry idempotency", settings(
                        engine=engine, timeout=.03, budget=.5), runner=lambda *_: calls.append(True) or [])
                self.assertEqual(result.server_calls[0]["outcome"], "client_deadline")
                self.assertFalse(result.deadline_exhausted)
                self.assertEqual(bool(calls), engine == "auto")
                self.assertEqual(result.status, "unavailable")
                if engine == "auto":
                    self.assertEqual(result.reason, "no keyword match; semantic search unavailable")
                    self.assertTrue(result.coverage["partial"])
            finally:
                release.set()

    def test_auto_server_stall_reserves_cli_and_source_time_within_one_budget(self):
        release = threading.Event()
        response = self.response({"lex": []})
        read = response.read
        response.read = lambda n: (release.wait(1), read(n))[1]
        queries = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth.md"
            path.write_text("OAuth retry requires deleting the stale token before requesting permission scope.\n")
            def runner(cmd, timeout):
                queries.append(cmd[cmd.index("search") + 1])
                time.sleep(min(.01, timeout * .7))
                return [item("qmd://notes/oauth.md", "OAuth retry")]
            try:
                with mock.patch("urllib.request.urlopen", return_value=response), \
                        mock.patch.object(recall, "local_path", return_value=path):
                    started = time.monotonic()
                    result = recall.recall("OAuth payment retry permission stale token", settings(
                        engine="auto", timeout=6, budget=.6, memory_collections=["agentmem"]), runner)
                self.assertLess(time.monotonic() - started, .7)
                self.assertEqual(result.status, "hit")
                self.assertEqual(result.attempted_engines, ["server", "cli"])
                self.assertEqual(len(queries[0].split()), 2)
                self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
                self.assertEqual(result.coverage["semantic"], "unavailable_cli")
                self.assertEqual(result.hits[0].anchor["start_line"], 1)
            finally:
                release.set()

    def test_healthy_semantic_server_keeps_priority_and_avoids_cli(self):
        row = item("qmd://notes/oauth.md", "OAuth retry")
        row["score"] = .9
        runner = mock.Mock(return_value=[])
        with mock.patch.object(recall, "server_post", return_value={"lex": [], "vec": [row]}), \
                mock.patch.object(recall, "local_path", return_value=None):
            result = recall.recall("Unexpected charges after connection failure", settings(
                engine="auto", budget=.3, rerank=False), runner)
        self.assertEqual(result.status, "hit")
        self.assertEqual(result.attempted_engines, ["server"])
        runner.assert_not_called()

    def test_outer_read_capacity_failure_retains_attempt_without_starting_transport(self):
        result = recall.Result(status="skipped", engine="server")
        with mock.patch.object(recall, "_READ_SLOTS", threading.BoundedSemaphore(0)), mock.patch("urllib.request.urlopen") as open_url:
            with self.assertRaises(TimeoutError):
                recall._server_attempt(result, "/search", time.monotonic() + 1,
                    lambda request_id: recall.server_post(settings(), "/search", {"request_id": request_id}, 1))
        self.assertEqual(result.server_calls[0]["outcome"], "recall_read_capacity")
        open_url.assert_not_called()

    def test_independent_requests_have_distinct_call_ids_and_diagnostics(self):
        from concurrent.futures import ThreadPoolExecutor
        with mock.patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: self.response({"lex": [], "vec": []})):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: recall.recall("payment retry idempotency", settings(engine="server")), range(2)))
        self.assertTrue(all(result.status == "miss" for result in results))
        self.assertNotEqual(results[0].server_calls[0]["request_id"], results[1].server_calls[0]["request_id"])
        results[0].server_calls[0]["outcome"] = "changed locally"
        self.assertEqual(results[1].server_calls[0]["outcome"], "response")

    def test_strict_server_failure_and_auto_cli_fallback_report_actual_attempts(self):
        for engine, expected in (("server", ["server"]), ("auto", ["server", "cli"])):
            with self.subTest(engine=engine), mock.patch("urllib.request.urlopen", side_effect=OSError("secret")):
                result = recall.recall("payment retry idempotency", settings(engine=engine), runner=lambda *_: [])
            self.assertEqual(result.engine, expected[-1])
            self.assertEqual(result.attempted_engines, expected)
            self.assertEqual(result.server_calls[0]["outcome"], "transport_error")
            self.assertEqual(result.to_dict()["server_calls"], result.server_calls)

    def test_outer_deadline_retains_attempt_and_does_not_borrow_abandoned_result(self):
        release = threading.Event()
        def stuck_pool(prompt, ladder, s, started, private_result, request_id):
            release.wait(1)
            private_result.server_calls.append({"outcome": "late result"})
            return {}
        try:
            with mock.patch.object(recall, "_server_pool", side_effect=stuck_pool):
                result = recall.recall("payment retry idempotency", settings(engine="server", budget=.03))
            self.assertEqual(result.engine, "server")
            self.assertEqual(result.attempted_engines, ["server"])
            self.assertEqual(result.server_calls[0]["outcome"], "recall_deadline")
            frozen = json.dumps(result.to_dict(), sort_keys=True)
            release.set()
            time.sleep(.03)
            self.assertEqual(json.dumps(result.to_dict(), sort_keys=True), frozen)
        finally:
            release.set()

    def test_failed_rerank_cannot_admit_scores_and_keeps_call_receipt(self):
        hits = [recall.Hit(uri=f"qmd://public/{i}.md", title=str(i), snippet="Public retry evidence", passage_id=f"p{i}") for i in range(2)]
        result = recall.Result(status="hit", engine="server")
        with mock.patch.object(recall, "server_post", return_value={"error": "deadline",
                "passage_scores": {"p0": .1, "p1": .9}, "scored_passage_ids": ["p0", "p1"]}):
            ranked = recall._rank_passages("retry", hits, settings(), result, time.monotonic() + 1)
        self.assertFalse(result.reranked)
        self.assertEqual([h.passage_id for h in ranked], ["p0", "p1"])
        self.assertEqual(result.server_calls[0]["route"], "/rerank")

    def test_hook_log_retains_failure_diagnostics_without_response_or_exception_payload(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(recall, "log_path", return_value=Path(tmp) / "recall.jsonl"):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("secret transport message")):
                result = recall.recall("payment retry idempotency", settings(engine="server"))
            recall.log_event("recall", result)
            row = json.loads((Path(tmp) / "recall.jsonl").read_text())
        self.assertEqual(row["engine"], "server")
        self.assertEqual(row["attempted_engines"], ["server"])
        self.assertEqual(row["server_calls"], result.server_calls)
        self.assertFalse(row["deadline_exhausted"])
        self.assertNotIn("secret", json.dumps(row))


class SentinelOrdinaryOutputTests(unittest.TestCase):
    """Claude Code puts a failed command's whole output in `error`; ordinary output in it is not the error."""

    def test_config_keys_are_not_errors_or_transient(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "for f in a b; do [ -f $f ] && jq .hooks $f; done"},
                   "error": 'Exit code 1\n=== a\n{\n  "timeout": 10,\n  "type": "command"\n}'}
        found = sentinel.analyse(payload, settings(), lambda c, t: [])
        self.assertEqual(found["line"], "Exit code 1")
        self.assertNotEqual(found["classification"]["class"], "transient")

    def test_shell_diagnostic_beats_earlier_prose(self):
        payload = {"tool_name": "Bash", "error": "Exit code 1\nThe user said permission is never needed for "
                   "pushes to the demo branch, and it was merged.\n\n(eval):1: ======== not found"}
        found = sentinel.analyse(payload, settings(), lambda c, t: [])
        self.assertEqual(found["line"], "(eval):1: ======== not found")

    def test_string_valued_error_keys_still_count(self):
        text = 'Exit code 1\n{\n  "error": "invalid_grant",\n  "retries": 3\n}'
        self.assertEqual(sentinel.error_line(text), '"error": "invalid_grant",')
        self.assertEqual(sentinel.classify_failure({}, sentinel.evidence(text))["class"], "authentication")

    def test_real_timeout_is_still_transient(self):
        found = sentinel.analyse({"tool_name": "Bash", "error": "Exit code 28\ncurl: (28) Operation timed out after 10001 ms"},
                                 settings(), lambda c, t: [])
        self.assertIn("timed out", found["line"])
        self.assertEqual(found["classification"]["class"], "transient")


class WrappedQmdTests(unittest.TestCase):
    """A `qmd` on PATH may be a shell script that execs nvm's node and the real CLI."""

    def test_sdk_and_node_come_from_the_wrapper_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node = root / "nvm" / "bin" / "node"
            package = root / "nvm" / "lib" / "node_modules" / "@tobilu" / "qmd"
            for path in (node, package / "bin" / "qmd", package / "dist" / "index.js", package / "dist" / "llm.js"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            node.chmod(0o755)
            wrapper = root / "local" / "bin" / "qmd"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text(f'#!/usr/bin/env bash\nexec "{node}" "{package / "bin" / "qmd"}" "$@"\n')
            self.assertEqual(setup_recall.qmd_sdk(str(wrapper)), (package / "dist" / "index.js").resolve())
            self.assertEqual(setup_recall.wrapper_node(str(wrapper)), str(node))

    def test_a_binary_qmd_has_no_wrapper_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "qmd"
            exe.write_bytes(b"\xcf\xfa\xed\xfe\0\0 exec /usr/bin/node")
            self.assertEqual(setup_recall.wrapper_targets(str(exe)), [])
            self.assertIsNone(setup_recall.wrapper_node(str(exe)))


class RealNodeTests(unittest.TestCase):
    """setup must record node itself, not a nodenv/nvm/asdf shim that fails under launchd."""

    def test_resolves_a_shim_to_the_real_binary(self):
        from claude_note import setup_recall
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "node-real"
            real.write_text("")
            done = mock.Mock(returncode=0, stdout=str(real) + "\n")
            with mock.patch.object(setup_recall.shutil, "which", return_value="/x/shims/node"), \
                 mock.patch.object(setup_recall.subprocess, "run", return_value=done):
                self.assertEqual(setup_recall.real_node(), str(real))

    def test_falls_back_to_path_node_when_the_probe_fails(self):
        from claude_note import setup_recall
        with mock.patch.object(setup_recall.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(setup_recall.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(setup_recall.real_node(), "/usr/bin/node")


class FuseScopeTests(unittest.TestCase):
    """`collections = []` means every collection; the memory collections must not narrow it."""

    def test_default_config_keeps_notes_from_every_collection(self):
        s = recall.Settings()
        s.collections, s.memory_collections = [], ["agentmem"]
        hits = [{"file": "qmd://notes/a.md", "title": "A", "score": .7},
                {"file": "qmd://agentmem/p/memory/b.md", "title": "B", "score": .6}]
        fused, best = recall.fuse([], hits, s)
        self.assertEqual({h["file"] for h in fused}, {h["file"] for h in hits})
        self.assertEqual(best, .7)

    def test_an_explicit_list_still_scopes_and_keeps_memory(self):
        s = recall.Settings()
        s.collections, s.memory_collections = ["notes"], ["agentmem"]
        hits = [{"file": "qmd://notes/a.md", "title": "A"}, {"file": "qmd://other/c.md", "title": "C"},
                {"file": "qmd://agentmem/p/memory/b.md", "title": "B"}]
        fused, _ = recall.fuse([], hits, s)
        self.assertEqual({h["file"] for h in fused}, {"qmd://notes/a.md", "qmd://agentmem/p/memory/b.md"})


class ServerIndexTests(unittest.TestCase):
    """The warm server only knows the default index; auto mode must not use it for another one."""

    def _run(self, index, engine):
        s = recall.Settings()
        s.engine, s.index, s.qmd, s.log = engine, index, "/bin/echo", False
        with mock.patch.object(recall, "_server_pool", return_value={}) as srv:
            recall.recall("why are customers charged twice on a retried payment", s, runner=lambda c, t: [])
        return srv.called

    def test_auto_skips_the_server_for_a_named_index(self):
        self.assertFalse(self._run("claude-note-doctor-1", "auto"))

    def test_auto_uses_the_server_for_the_default_index(self):
        self.assertTrue(self._run("", "auto"))

    def test_explicit_server_engine_is_honoured(self):
        self.assertTrue(self._run("cn-demo2", "server"))


class SqliteFallbackTests(unittest.TestCase):
    def test_default_auto_fallback_verifies_source_without_starting_a_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.md"
            path.write_text("Design of everyday things: visible actions, feedback and recoverable errors.\n")
            batch = {"complete": True, "reason": "", "calls": [
                (None, False, 2, [item("qmd://notes/book.md", "Design of everyday things", "")])]}
            with mock.patch.object(recall, "server_post", return_value={"error": "transport_error"}), \
                    mock.patch("claude_note.recall_lexical.search_batch", return_value=batch) as search, \
                    mock.patch.object(recall, "local_path", return_value=path), \
                    mock.patch.object(recall, "_process") as run:
                result = recall.recall("knowledge design everyday", settings(engine="auto"))
            self.assertEqual(result.status, "hit")
            self.assertEqual(result.engine, "sqlite")
            self.assertEqual(result.attempted_engines, ["server", "sqlite"])
            self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
            self.assertEqual(result.coverage["lexical"], "complete_sqlite_scoped_before_limit")
            self.assertTrue(result.coverage["partial"])
            self.assertEqual(search.call_count, 1)
            self.assertLessEqual(search.call_args.args[4] - time.monotonic(), 2.0)
            run.assert_not_called()

    def test_missing_sqlite_adapter_keeps_real_cli_compatibility_fallback(self):
        with mock.patch.object(recall, "server_post", return_value={"error": "transport_error"}), \
                mock.patch("claude_note.recall_lexical.search_batch", return_value=None), \
                mock.patch.object(recall, "_process", return_value=(0, '[]')) as run:
            result = recall.recall("design everyday things", settings(engine="auto"))
        self.assertEqual(result.engine, "cli")
        self.assertEqual(result.attempted_engines, ["server", "sqlite", "cli"])
        self.assertEqual(result.status, "unavailable")
        self.assertIn("semantic search unavailable", result.reason)
        self.assertTrue(run.called)

    def test_partial_sqlite_miss_is_unavailable_within_original_budget(self):
        batch = {"complete": False, "reason": "deadline", "calls": []}
        with mock.patch.object(recall, "server_post", return_value={"error": "transport_error"}), \
                mock.patch("claude_note.recall_lexical.search_batch", return_value=batch), \
                mock.patch.object(recall, "_process") as run:
            result = recall.recall("design everyday things", settings(engine="auto"))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.engine, "sqlite")
        self.assertIn("SQLite keyword", result.reason)
        self.assertTrue(result.coverage["partial"])
        run.assert_not_called()
