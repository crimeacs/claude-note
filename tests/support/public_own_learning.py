"""Public offline bridge component; injected extraction/vector data, no utility claim.

Application imports are lazy and occur inside public_fixture after owned home/
config lookups and reversible transport guards are installed. The real HOME
environment value is preserved. This component never starts a service or indexer.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock

def load_application():
    """Import only after public_fixture has isolated configuration lookups."""
    global agent_tasks, config, egress, freshness, import_publication, knowledge_pack
    global longitudinal, mutation, note_router, note_writer, queue_manager
    global session_tracker, synthesizer, transcript_reader, vault_indexer, worker
    from claude_note import (agent_tasks, config, egress, freshness, import_publication,
        knowledge_pack, longitudinal_tasks as longitudinal, mutation, note_router,
        note_writer, queue_manager, session_tracker, synthesizer, transcript_reader,
        vault_indexer, worker)
    return SimpleNamespace(**{name: globals()[name] for name in (
        "agent_tasks", "config", "egress", "freshness", "import_publication",
        "knowledge_pack", "longitudinal", "mutation", "note_router", "note_writer",
        "queue_manager", "session_tracker", "synthesizer", "transcript_reader",
        "vault_indexer", "worker")})



def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


class BridgeError(ValueError):
    pass


class PublicBridge:
    """One actual publication/capture/mutation chain per arm and phase.

    Hashes are integrity bindings within the isolated fixture, not signatures or
    host-delivery attestations. Arbitrary hostile Python remains out of scope.
    """
    def __init__(self, root, fixture, *, run_id="public-bridge", arms=("dense_only",)):
        self.app = load_application()
        self.root = Path(root).resolve()
        self.fixture = fixture
        self.spec = longitudinal.Spec(trajectories=(fixture["trajectory"],), arms=arms)
        self.journal = longitudinal.PhaseJournal(self.root, self.spec, run_id)
        self.adapter_revision = digest(Path(__file__).read_bytes())
        self.transport_calls = 0
        self.delivery_calls = 0
        self.bundles = {}
        self._callback_references = None

    def close(self):
        self.journal.close()

    def cell(self, arm, phase):
        return longitudinal.Cell(self.fixture["trajectory"], arm, phase)

    def area(self, cell):
        return self.root / "artifacts" / cell.trajectory / cell.arm / ("phase-" + str(cell.phase))

    def vault(self, cell):
        return self.root / "artifacts" / cell.trajectory / cell.arm / "vault"

    def artifact(self, path):
        path = Path(path)
        if (not path.is_absolute() or path != path.resolve() or not path.is_file()
                or not path.is_relative_to(self.root)):
            raise BridgeError("nonfixture artifact")
        return longitudinal.Artifact(path.relative_to(self.root).as_posix(), digest(path.read_bytes()))

    def save(self, path, value):
        write_new(path, encoded(value) + b"\n")
        return self.artifact(path)

    def ref(self, cell, stage, path):
        return self.journal.reference(cell, stage, self.artifact(path).source_id)

    def stage_path(self, cell, stage):
        names = {"act": "task-output.jsonl", "capture": "capture.json",
                 "extract": "extraction.json", "mutate": "mutation.json", "index": "simulated-index.json"}
        return self.area(cell) / names[stage]

    @staticmethod
    def decode_reference(value):
        if set(value) != {"run_id", "cell", "stage", "artifact", "receipt_digest"}:
            raise longitudinal.ReferenceError("invalid producer reference fields")
        return longitudinal.Reference(value["run_id"], longitudinal.Cell(**value["cell"]),
            value["stage"], longitudinal.Artifact(**value["artifact"]), value["receipt_digest"])

    def authenticate(self, cell, stage, references):
        """Resolve exact direct AND transitive producers before callback entry.

        run_stage independently repeats its direct-reference check under lock.
        No authentication cache is populated from callback-asserted metadata.
        """
        verified = set()
        edges = {"act": (), "capture": ("input_receipt",), "extract": ("capture_receipt",),
                 "mutate": ("capture_receipt", "extract_receipt"), "index": ("mutation_receipt",)}
        def visit(target, target_stage, reference):
            if (not isinstance(reference, longitudinal.Reference) or reference.run_id != self.journal.run_id or
                    reference.cell.trajectory != target.trajectory or reference.cell.arm != target.arm or
                    reference.cell.phase > target.phase or (reference.cell.phase == target.phase and
                    longitudinal.STAGES.index(reference.stage) >= longitudinal.STAGES.index(target_stage))):
                raise longitudinal.ReferenceError("cross-arm/run or future producer")
            if reference in verified:
                return
            path = self.stage_path(reference.cell, reference.stage)
            if reference.artifact.source_id != path.relative_to(self.root).as_posix():
                raise longitudinal.ReferenceError("unexpected producer artifact identity")
            try:
                expected = self.journal.reference(reference.cell, reference.stage, reference.artifact.source_id)
            except longitudinal.JournalError as error:
                raise longitudinal.ReferenceError("producer receipt unresolved") from error
            if reference != expected or reference.artifact != self.artifact(path):
                raise longitudinal.ReferenceError("producer receipt or current bytes differ")
            verified.add(reference)
            if edges[reference.stage]:
                record = json.loads(path.read_bytes())
                for field in edges[reference.stage]:
                    visit(reference.cell, reference.stage, self.decode_reference(record[field]))
        for reference in references:
            visit(cell, stage, reference)
        return frozenset(verified)

    def exact(self, cell, stage, reference):
        # Called only while the product holds its non-reentrant journal lock.
        # The immutable pre-callback snapshot authenticates the entire edge;
        # this second check binds the current bytes without reentering journal.
        path = self.stage_path(cell, stage)
        actual = self.artifact(path)
        if (self._callback_references is None or reference not in self._callback_references or
                reference.run_id != self.journal.run_id or reference.cell != cell or reference.stage != stage or
                reference.artifact != actual):
            raise BridgeError("producer receipt or current bytes differ")
        return path

    @contextmanager
    def owned_config(self, cell):
        root = self.vault(cell)
        root.mkdir(parents=True, exist_ok=True)
        settings = {"VAULT_ROOT": root, "CLAUDE_NOTE_DIR": root / ".claude-note",
            "STATE_DIR": root / ".claude-note/state", "QUEUE_DIR": root / ".claude-note/queue",
            "LOGS_DIR": root / ".claude-note/logs", "INBOX_PATH": root / "inbox.md",
            "INDEX_PATH": root / ".claude-note/state/vault_index.json",
            "LITERATURE_DIR": root / "literature", "INTERNAL_DIR": root / "internal",
            "OPEN_QUESTIONS_FILE": root / "open-questions.md",
            "SYNTH_MODE": "route", "SYNTH_MODEL": "injected-public-no-model",
            "SYNTH_RESUME_AFTER": "", "QMD_SYNTH_ENABLED": False,
            "QMD_LINK_ENHANCE_ENABLED": False, "DEBOUNCE_SECONDS": 0}
        # This fictional public-only policy is never installed or persisted.
        public_policy = {"default": ["local", "host"], "rules": [
            {"source": str(self.root), "audiences": ["local", "host", "model"]},
            {"source": str(self.root) + "/*", "audiences": ["local", "host", "model"]}]}
        with ExitStack() as stack:
            for name, value in settings.items():
                stack.enter_context(mock.patch.object(config, name, value))
            stack.enter_context(mock.patch.object(egress, "policy", return_value=public_policy))
            stack.enter_context(mock.patch("claude_note.provenance.author_email", return_value="public@example.invalid"))
            stack.enter_context(mock.patch.object(worker, "_capture_only", True))
            yield root

    def stage(self, cell, stage, callback, references=()):
        direct = tuple(references)
        authenticated = self.authenticate(cell, stage, direct)
        def locked_callback(context):
            if self._callback_references is not None or context.references != direct:
                raise BridgeError("callback reference context differs")
            self._callback_references = authenticated
            try:
                return callback(context)
            finally:
                self._callback_references = None
        return self.journal.run_stage(cell, stage, locked_callback,
            adapter_revision=self.adapter_revision, references=references)

    def act(self, cell):
        def callback(context):
            text = self.fixture["phase_outputs"][cell.phase]
            rows = [{"type": "user", "message": {"content": self.fixture["request"]}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
            path = self.area(cell) / "task-output.jsonl"
            write_new(path, b"".join(encoded(row) + b"\n" for row in rows))
            return longitudinal.Outcome(details={"input": "authored_public_not_agent_output", "scored": False},
                artifacts=(self.artifact(path),))
        return self.stage(cell, "act", callback)

    def capture(self, cell, act_ref):
        def callback(context):
            source = self.exact(cell, "act", act_ref)
            with self.owned_config(cell) as vault:
                sid = "bridge-" + str(cell.phase) + "-" + cell.arm
                publication = import_publication.publish("claude-code", sid,
                    "public-" + str(cell.phase), str(source), source.read_bytes(),
                    str(self.area(cell)), self.fixture["request"])
                events = list(queue_manager.read_events(publication.queue_path))
                applied = worker.process_session(sid, events, logging.getLogger("public-bridge"))
                state = session_tracker.load_session_state(sid)
                if not applied:
                    return longitudinal.Outcome(status="failed",
                        details=self.capture_diagnostic(state, publication.event_ids))
                transcript = transcript_reader.read_transcript_from_state(state)
                snapshot = Path(transcript.snapshot_path)
                if (snapshot.read_bytes() != source.read_bytes() or
                        transcript.source_revision != act_ref.artifact.revision or
                        set(publication.event_ids) - set(state.processed_event_ids)):
                    raise BridgeError("capture evidence differs")
                value = {"run_id": self.journal.run_id, "cell": cell.to_dict(),
                    "input_receipt": act_ref.to_dict(), "session_id": sid,
                    "publication_id": publication.publication_id,
                    "publication_receipt": self.artifact(import_publication._receipt(publication.publication_id)).to_dict(),
                    "queue_batch": self.artifact(publication.queue_path).to_dict(),
                    "snapshot": self.artifact(snapshot).to_dict(),
                    "capture_note": self.artifact(note_writer.get_note_path(state)).to_dict(),
                    "source_revision": transcript.source_revision,
                    "anchors": [event.anchor for event in transcript.source_events],
                    "mechanism": "persistent_import_publication_then_capture_only_worker_and_verified_bound_snapshot"}
                artifact = self.save(self.area(cell) / "capture.json", value)
                return longitudinal.Outcome(details={"provider_calls": 0}, artifacts=(artifact,))
        return self.stage(cell, "capture", callback, (act_ref,))

    @staticmethod
    def capture_diagnostic(state, event_ids):
        """Fixed public-stage codes/counts only; no source/error text is logged."""
        expected = set(event_ids)
        processed = set(state.processed_event_ids) & expected if state else set()
        filtered = {key: value for key, value in state.filtered_event_ids.items()
                    if key in expected} if state else {}
        recursive = sum(value == "recursion-policy" for value in filtered.values())
        reason = ("public_capture_state_unavailable" if state is None else
            "public_capture_recursion_policy" if expected and recursive == len(expected) else
            "public_capture_note_not_applied")
        return {"reason": reason, "expected_event_count": len(expected),
                "processed_event_count": len(processed), "filtered_event_count": len(filtered),
                "recursion_filtered_count": recursive, "provider_calls": 0}

    def captured(self, cell, capture_ref):
        path = self.exact(cell, "capture", capture_ref)
        record = json.loads(path.read_bytes())
        self.exact(cell, "act", self.decode_reference(record["input_receipt"]))
        for key in ("publication_receipt", "queue_batch", "snapshot", "capture_note"):
            item = longitudinal.Artifact(**record[key])
            path = self.root / item.source_id
            if self.artifact(path) != item:
                raise BridgeError("captured producer artifact changed")
        return record

    def extract(self, cell, capture_ref, *, wrong_span=False, fail_transport=False):
        def callback(context):
            capture = self.captured(cell, capture_ref)
            with self.owned_config(cell):
                state = session_tracker.load_session_state(capture["session_id"])
                transcript = transcript_reader.read_transcript_from_state(state)
                anchors = [event.anchor for event in transcript.source_events]
                if anchors != capture["anchors"] or transcript.source_revision != capture["source_revision"]:
                    raise BridgeError("extraction source differs from capture")
                # The returned lesson is derived from this capture's actual parsed
                # assistant text, never a shared/prewritten learned-memory file.
                body = "\n\n".join(transcript.assistant_texts)
                if not body:
                    raise BridgeError("no captured task output")
                response = {"session_id": "untrusted-model-field", "date": "2026-10-27",
                    "title": "Proposed public observation", "note_ops": [{"op": "create",
                    "path": "memory/phase-" + str(cell.phase) + ".md", "frontmatter": {"type": "gotcha"},
                    "body_markdown": body, "managed_block_id": "public-phase-" + str(cell.phase),
                    "source_spans": ["synthetic://uncaptured#L1@" + "0" * 64] if wrong_span else anchors}]}
                def deterministic_transport(command, **kwargs):
                    self.transport_calls += 1
                    if (command[0] != "/public-injected-extractor-never-execute" or
                            not all(anchor in command[-1] for anchor in anchors) or body not in command[-1]):
                        raise BridgeError("unexpected extraction transport")
                    if fail_transport:
                        raise RuntimeError("authored public extraction interruption")
                    return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
                with mock.patch.object(synthesizer, "_claude_bin", return_value="/public-injected-extractor-never-execute"), \
                     mock.patch.object(synthesizer.subprocess, "run", side_effect=deterministic_transport):
                    pack = synthesizer.synthesize_from_state(state, vault_indexer.VaultIndex())
                    # Exercise the actual extraction cache as well as the outer
                    # journal replay; a cache hit must not call the transport.
                    before = self.transport_calls
                    cached = synthesizer.synthesize_from_state(state, vault_indexer.VaultIndex())
                    if self.transport_calls != before or cached.to_dict() != pack.to_dict():
                        raise BridgeError("real extraction cache did not replay exactly")
                if not pack or knowledge_pack.validate_knowledge_pack(pack):
                    raise BridgeError("invalid persisted extraction")
                session_tracker.save_session_state(state)
                persisted = session_tracker.load_session_state(state.session_id)
                if (persisted.extraction_generation != pack.generation or
                        persisted.extraction_checkpoint != state.extraction_checkpoint):
                    raise BridgeError("extraction checkpoint was not durably saved")
                cache = config.STATE_DIR / "extractions" / (pack.generation + ".json")
                record = {"capture_receipt": capture_ref.to_dict(), "cache": self.artifact(cache).to_dict(),
                    "state": self.artifact(session_tracker.get_state_file(state.session_id)).to_dict(),
                    "extraction_generation": state.extraction_generation,
                    "extraction_checkpoint": state.extraction_checkpoint, "real_cache_reuse_without_transport": True,
                    "pack": pack.to_dict(), "extractor": "injected_deterministic_transport_not_a_model"}
                artifact = self.save(self.area(cell) / "extraction.json", record)
                return longitudinal.Outcome(details={"extraction": "real_parse_provenance_validation_and_cache_with_injected_transport"}, artifacts=(artifact,))
        return self.stage(cell, "extract", callback, (capture_ref,))

    def extracted(self, cell, capture_ref, extraction_ref):
        record = json.loads(self.exact(cell, "extract", extraction_ref).read_bytes())
        if record["capture_receipt"] != capture_ref.to_dict():
            raise BridgeError("extraction has different capture")
        for key in ("cache", "state"):
            item = longitudinal.Artifact(**record[key])
            if self.artifact(self.root / item.source_id) != item:
                raise BridgeError("extraction cache or durable state changed")
        pack, state = record["pack"], json.loads((self.root / record["state"]["source_id"]).read_bytes())
        if (json.loads((self.root / record["cache"]["source_id"]).read_bytes()) != pack or
                state["extraction_generation"] != pack["generation"] or
                record["extraction_generation"] != pack["generation"] or
                state["extraction_checkpoint"] != record["extraction_checkpoint"] or
                record["extraction_checkpoint"]["revision"] != pack["source_revision"]):
            raise BridgeError("pack cache and checkpoint differ")
        return record

    def mutate(self, cell, capture_ref, extraction_ref):
        def callback(context):
            self.captured(cell, capture_ref)
            record = self.extracted(cell, capture_ref, extraction_ref)
            pack = knowledge_pack.KnowledgePack.from_dict(record["pack"])
            with self.owned_config(cell) as vault:
                # This first gate creates a new per-phase artifact. Production
                # append-to-human-file semantics are deliberately not an input.
                target = mutation.resolve_note_path(pack.note_ops[0].path, vault)
                if target.exists():
                    raise BridgeError("phase target predates this learned mutation")
                outcome = note_router._apply_note_op_result(pack.note_ops[0], vault,
                    session_id=pack.session_id, assistant=pack.assistant)
                if outcome is None or not outcome.applied:
                    raise BridgeError("mutation was not applied")
                effect = mutation._metadata(vault, "effects", mutation.operation_id(
                    mutation.operation_id(outcome.path.relative_to(vault).as_posix()), pack.note_ops[0].operation_id))
                if not effect or effect.get("status") != "applied" or effect.get("revision") != outcome.revision:
                    raise BridgeError("exact mutation effect receipt absent")
                value = {"extract_receipt": extraction_ref.to_dict(), "capture_receipt": capture_ref.to_dict(),
                    "learned": self.artifact(outcome.path).to_dict(), "operation_id": pack.note_ops[0].operation_id,
                    "mutation_status": outcome.status, "effect_record": effect,
                    "saved_generation": freshness.snapshot(vault)["saved_generation"]}
                artifact = self.save(self.area(cell) / "mutation.json", value)
                return longitudinal.Outcome(artifacts=(artifact,))
        return self.stage(cell, "mutate", callback, (capture_ref, extraction_ref))

    def index(self, cell, capture_ref, extraction_ref, mutation_ref, *, active=True, revision=None):
        def callback(context):
            self.captured(cell, capture_ref)
            self.extracted(cell, capture_ref, extraction_ref)
            record = json.loads(self.exact(cell, "mutate", mutation_ref).read_bytes())
            if record["extract_receipt"] != extraction_ref.to_dict() or record["capture_receipt"] != capture_ref.to_dict():
                raise BridgeError("mutation chain differs")
            learned = longitudinal.Artifact(**record["learned"])
            if self.artifact(self.root / learned.source_id) != learned or not active or revision not in (None, learned.revision):
                raise BridgeError("simulated indexed revision inactive or different")
            with self.owned_config(cell) as vault:
                saved = freshness.snapshot(vault)
                if saved["saved_generation"] != record["saved_generation"]:
                    raise BridgeError("simulated index generation stale")
                retained = []
                for name, entry in sorted(saved["retained_revisions"].items()):
                    actual = self.artifact(vault / name)
                    if entry["status"] != "saved" or actual.revision != entry["revision"]:
                        raise BridgeError("simulated retained index revision differs")
                    retained.append({"artifact": actual.to_dict(), "active": True})
                owner = digest(encoded(["SIMULATION", self.journal.run_id, cell.trajectory, cell.arm]))
                scope = digest(encoded(["SIMULATION", retained, saved["saved_generation"]]))
                for capability in ("lexical", "vector"):
                    freshness.acknowledge(saved["saved_generation"], capability=capability,
                        vault_root=vault, index_owner=owner, proof_scope=scope)
                value = {"simulation": True, "native_index_or_query_readiness_proven": False,
                    "mutation_receipt": mutation_ref.to_dict(), "learned": learned.to_dict(),
                    "active": True, "generation": saved["saved_generation"], "owner": owner, "scope": scope,
                    "retained_simulated_rows": retained, "freshness": freshness.snapshot(vault)}
                artifact = self.save(self.area(cell) / "simulated-index.json", value)
                return longitudinal.Outcome(details={"proof": "simulated_public_metadata_only"}, artifacts=(artifact,))
        return self.stage(cell, "index", callback, (capture_ref, extraction_ref, mutation_ref))

    def bundle(self, cell):
        names = {"capture": "capture.json", "extract": "extraction.json", "mutate": "mutation.json", "index": "simulated-index.json"}
        return {stage: self.ref(cell, stage, self.area(cell) / name) for stage, name in names.items()}

    def retrieve(self, cell, prior=None):
        def callback(context):
            if cell.arm == "no_memory":
                if prior: raise BridgeError("no-memory cannot consume learned memory")
                return longitudinal.Outcome(details={"context": "", "mechanism": "no_memory"})
            if cell.arm == "static_instructions":
                if prior: raise BridgeError("static control cannot consume learned memory")
                return longitudinal.Outcome(details={"context": self.fixture["static_instruction"], "mechanism": "static_only"})
            if prior is None:
                if cell.phase: raise BridgeError("prior own-memory chain required")
                return longitudinal.Outcome(details={"context": "", "mechanism": "initial_empty"})
            producer = self.cell(cell.arm, cell.phase - 1)
            capture = self.captured(producer, prior["capture"])
            if cell.arm == "bounded_history":
                if set(prior) != {"capture"}:
                    raise BridgeError("history control cannot consume learned memory")
                snapshot = self.root / capture["snapshot"]["source_id"]
                text = snapshot.read_text()[:4096]
                return longitudinal.Outcome(details={"context": text, "mechanism": "own_captured_history_only"})
            if cell.arm != "dense_only":
                raise BridgeError("this component implements only the dense-only learning adapter")
            extraction = self.extracted(producer, prior["capture"], prior["extract"])
            change = json.loads(self.exact(producer, "mutate", prior["mutate"]).read_bytes())
            indexed = json.loads(self.exact(producer, "index", prior["index"]).read_bytes())
            if (extraction["capture_receipt"] != prior["capture"].to_dict() or
                    change["extract_receipt"] != prior["extract"].to_dict() or
                    change["capture_receipt"] != prior["capture"].to_dict() or
                    indexed["mutation_receipt"] != prior["mutate"].to_dict() or
                    not indexed["simulation"] or not indexed["active"] or change["learned"] != indexed["learned"]):
                raise BridgeError("incomplete own-learning chain")
            learned = longitudinal.Artifact(**indexed["learned"])
            path = self.root / learned.source_id
            if self.artifact(path) != learned:
                raise BridgeError("learned artifact changed")
            for row in indexed["retained_simulated_rows"]:
                item = longitudinal.Artifact(**row["artifact"])
                if row["active"] is not True or self.artifact(self.root / item.source_id) != item:
                    raise BridgeError("retained simulated revision changed")
            with self.owned_config(producer) as vault:
                current = freshness.snapshot(vault)
                if (current["saved_generation"] != indexed["generation"] or
                        any(current[key] != indexed["freshness"][key] for key in
                            ("lexical_generation", "vector_generation", "index_owner", "lexical_proof_scope", "vector_proof_scope"))):
                    raise BridgeError("freshness proof changed")
            body = path.read_text()
            collection = "public-" + cell.arm.replace("_", "-")
            uri = "qmd://" + collection + "/" + path.name
            # A deterministic first managed passage is a simulated vector row,
            # not an answer-based selector; exact offsets refer to the real file.
            marker = "<!-- claude-note:public-phase-" + str(producer.phase) + ":start -->"
            start = body.index("\n", body.index(marker)) + 1
            end = body.index("\n\n<!-- claude-note:provenance", start)
            excerpt, prefix = body[start:end], body[:start]
            anchor = {"start_char": len(prefix.encode("utf-16-le")) // 2,
                      "end_char": len(body[:end].encode("utf-16-le")) // 2,
                      "start_line": prefix.count("\n") + 1, "end_line": body[:end].count("\n") + 1}
            text = re.sub(r"\s+", " ", re.sub(r"^#+\s*", "", excerpt, flags=re.M)).strip()
            passage = digest(json.dumps([uri, learned.revision, anchor, text], ensure_ascii=False, separators=(",", ":")).encode())
            response = {"manifest": {"ok": True, "protocol_version": 2, "index": {"id": "default"},
                "vector_ready": True, "models": {"embed": {"id": self.fixture["simulated_embed_identity"], "ready": True}}},
                "coverage": {"semantic": "complete_scoped_exact_vectors"}, "partial": False, "lex": [],
                "vec": [{"file": uri, "source_id": uri, "revision": learned.revision,
                    "score": .5, "lexical_evidence": False, "scored_text": text,
                    "anchor": anchor, "passage_id": passage}]}
            result = agent_tasks.dense_result(response, collection=collection,
                documents={uri: body}, expected_model=self.fixture["simulated_embed_identity"])
            if not result["available"] or not result["evidence"]:
                raise BridgeError("next-phase adapter rejected own artifact")
            self.delivery_calls += 1
            return longitudinal.Outcome(details={"adapter": "real_dense_result_simulated_vector_response",
                "context": result["context"], "source_revision": learned.revision,
                "capture_revision": capture["source_revision"], "reranked": result["reranked"],
                "coverage": result["coverage"], "measured_learning": False})
        refs = tuple(prior.values()) if prior else ()
        return self.stage(cell, "retrieve", callback, refs)


@contextmanager
def public_fixture():
    """One fresh public-only test fixture, with reversible process state.

    Ordinary test discovery imports this helper without reading app config.
    Transport mocks fail closed for this fixture; an independently reviewed
    runner supplies process-wide filesystem/native supervision when required.
    These mocks and paths are not an OS sandbox against arbitrary Python.
    """
    original_home = os.environ.get("HOME")
    calls = {"transport": 0}

    def deny_transport(*args, **kwargs):
        calls["transport"] += 1
        raise AssertionError("public fixture attempted external transport")

    # /tmp is the common POSIX temporary parent, including when TMPDIR names a
    # repository with a self-capture marker. Resolve its real path; never record
    # a fictional cwd to avoid the production recursion policy.
    parent = Path("/tmp").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="public-memory-chain-", dir=parent) as temp:
        root = Path(temp).resolve(strict=True)
        identity = root.lstat()
        if (not stat.S_ISDIR(identity.st_mode) or identity.st_uid != os.getuid()
                or stat.S_IMODE(identity.st_mode) != 0o700):
            raise BridgeError("public fixture root is not private and owned")
        for name in ("home", "config", "cache", "data", "state", "tmp", "bootstrap-vault"):
            (root / name).mkdir(mode=0o700)
        home = root / "home"

        def public_expanduser(path):
            text = os.fsdecode(path)
            if text == "~":
                return str(home)
            if text.startswith("~/"):
                return str(home / text[2:])
            if text.startswith("~"):
                raise BridgeError("other-user home forbidden in public fixture")
            return path

        environment = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "TMPDIR": str(root / "tmp"),
            "CLAUDE_NOTE_VAULT": str(root / "bootstrap-vault"),
            "CLAUDE_NOTE_AUTHOR": "public@example.invalid",
            "CLAUDE_NOTE_QMD_SYNTH_ENABLED": "false",
            "CLAUDE_NOTE_QMD_LINK_ENHANCE_ENABLED": "false",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, environment))
            stack.enter_context(mock.patch.object(Path, "home", return_value=home))
            stack.enter_context(mock.patch.object(os.path, "expanduser", side_effect=public_expanduser))
            stack.enter_context(mock.patch.object(tempfile, "tempdir", str(root / "tmp")))
            descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, descriptor)
            stack.callback(os.fchdir, descriptor)
            os.chdir(root)
            for module, name in ((subprocess, "Popen"), (subprocess, "run"),
                                 (socket, "socket"), (sqlite3, "connect"), (os, "system")):
                stack.enter_context(mock.patch.object(module, name, side_effect=deny_transport))
            for name in dir(os):
                if name.startswith(("exec", "spawn")) or name in ("fork", "forkpty", "posix_spawn", "posix_spawnp"):
                    if callable(getattr(os, name)):
                        stack.enter_context(mock.patch.object(os, name, side_effect=deny_transport))
            app = load_application()
            if any(marker.lower() in str(root).lower() for marker in app.config.RECURSION_MARKERS):
                raise BridgeError("actual public fixture cwd triggers self-capture policy")
            fixture_path = Path(__file__).resolve().parents[1] / "fixtures/public_own_learning.json"
            fixture = json.loads(fixture_path.read_bytes())
            try:
                yield SimpleNamespace(root=root, fixture=fixture, app=app, calls=calls)
            finally:
                if calls["transport"]:
                    raise AssertionError("public fixture transport guard was entered")
        if os.environ.get("HOME") != original_home:
            raise AssertionError("public fixture changed inherited HOME")
