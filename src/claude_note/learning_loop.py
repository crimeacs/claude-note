"""Explicit public experience -> proposed memory, without a worker or service.

The caller supplies actual task/action/verifier bytes and an extraction callback.
This module cannot establish that a string came from an agent, that a verifier
is correct, or that a model's lesson is true. It never attests a lesson. The
callback is trusted Python, not sandboxed; its provider cost/deadline belongs to
the caller. No configured vault, index, model, HOME, or source policy is changed.

Capture uses the real transcript parser, confined mutation writer and restrictive
lineage ledger. Completed learning receipts select immutable Markdown revisions;
supersession changes that selection and preserves all earlier evidence. Local
lexical selection is not QMD, semantic indexing, or verified host delivery.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from types import MappingProxyType

from . import egress, mutation, transcript_reader

MAX_OBSERVATIONS = 128
MAX_RECORD_BYTES = 262144
MAX_EXPERIENCE_BYTES = 65536
MAX_LESSONS = 16
MAX_LESSON_CHARS = 4096
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SCOPE_FIELDS = frozenset(("repository", "environment", "company_id", "workflow"))


class LearningLoopError(ValueError):
    """Invalid ownership, evidence, scope, replay or bounded public input."""


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise LearningLoopError("Invalid bounded JSON") from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise LearningLoopError("Learning record exceeds byte bound")
    return raw


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _identity(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LearningLoopError("Invalid learning identity")
    return value


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _scope(value):
    if value is None:
        return {}
    if type(value) is not dict or set(value) - SCOPE_FIELDS:
        raise LearningLoopError("Unknown learning scope")
    if any(not isinstance(v, str) or not v or len(v) > 256 or
           any(ord(c) < 32 or ord(c) == 127 for c in v) for v in value.values()):
        raise LearningLoopError("Invalid learning scope value")
    return dict(value)


class LearningLoop:
    """One opt-in public run/arm; calls are serial and never auto-retry effects.

    ``observe(task_id, task, actual_response, verifier_feedback, scope=None)``
    returns an exact revision-bound observation receipt. ``extractor(request)``
    receives a recursively immutable view of that capture and earlier applicable
    memory. It returns exactly ``{"lessons": [{"key": ..., "text": ...,
    "source_spans": [...], "supersedes": [...], "scope": {...}}]}``; the last
    two fields are optional. Spans must belong to this capture, scope cannot be
    widened, and no authority/approval fields are accepted.

    ``learn(receipt)`` durably records completed/failed outcomes; a partial intent
    forbids automatic callback replay. ``retrieve(task, before_sequence=N)`` uses
    only completed learned revisions from observations strictly before N. A
    frozen-memory control keeps that cutoff while continuing to observe tasks.
    """

    def __init__(self, root, *, run_id, arm_id, extractor, public_only,
                 repository="public-demo", environment="test",
                 max_memory_chars=4096, scope=None):
        if public_only is not True:
            raise LearningLoopError("Explicit public-only opt-in required")
        if not callable(extractor):
            raise LearningLoopError("Explicit extractor callback required")
        if type(max_memory_chars) is not int or not 1 <= max_memory_chars <= 16384:
            raise LearningLoopError("Invalid memory character bound")
        self.run_id, self.arm_id = _identity(run_id), _identity(arm_id)
        self.scope = _scope({"repository": repository, "environment": environment})
        extra = _scope(scope)
        if any(k in self.scope and self.scope[k] != v for k, v in extra.items()):
            raise LearningLoopError("Constructor scope changes owner context")
        self.scope.update(extra)
        self.extractor, self.max_memory_chars = extractor, max_memory_chars
        supplied = Path(root)
        if (not supplied.is_absolute() or ".." in supplied.parts or
                any(part.is_symlink() for part in (supplied, *supplied.parents))):
            raise LearningLoopError("Explicit unlinked absolute root required")
        self.root = supplied.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(part.is_symlink() for part in (supplied, *supplied.parents)):
            raise LearningLoopError("Learning root ancestor changed")
        if not self.root.is_dir():
            raise LearningLoopError("Learning root is not a directory")
        self._mutex, self._fd = threading.Lock(), None
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._manifest = {"version": 1, "kind": "public-learning-loop",
                          "run_id": self.run_id, "arm_id": self.arm_id,
                          "scope": self.scope, "max_memory_chars": max_memory_chars}
        try:
            # A new interface cannot silently adopt an existing private vault.
            if not (self.root / "manifest.json").exists() and any(self.root.iterdir()):
                raise LearningLoopError("Existing root lacks an owned loop manifest")
            self._fd = os.open(self.root / ".learning-loop.lock",
                               os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LearningLoopError("Invalid learning lock")
            with self._locked(check_manifest=False):
                existing = self._read("manifest.json")
                if existing is None:
                    self._save("manifest.json", self._manifest)
                elif existing != self._manifest:
                    raise LearningLoopError("Learning run/arm/context identity changed")
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self._mutex.acquire(blocking=False):
            raise LearningLoopError("Learning loop is in use")
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
        finally:
            self._mutex.release()

    def _live_lock(self):
        """An open descriptor for an unlinked lock cannot admit another effect."""
        try:
            opened = os.fstat(self._fd)
            current = (self.root / ".learning-loop.lock").lstat()
            if (not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(opened.st_mode) or
                    opened.st_nlink != 1 or current.st_nlink != 1 or
                    (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                raise LearningLoopError("Learning lock was retired or replaced")
        except (OSError, TypeError) as exc:
            raise LearningLoopError("Learning lock is unavailable") from exc

    @contextmanager
    def _locked(self, *, check_manifest=True):
        if not self._mutex.acquire(blocking=False):
            raise LearningLoopError("Learning loop is in use")
        acquired = False
        try:
            if self._fd is None or self._root_fd is None:
                raise LearningLoopError("Learning loop is closed")
            current, opened = self.root.lstat(), os.fstat(self._root_fd)
            if (not stat.S_ISDIR(current.st_mode) or
                    (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                raise LearningLoopError("Learning root changed")
            self._live_lock()
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise LearningLoopError("Learning loop is in use") from exc
            self._live_lock()
            if check_manifest and self._read("manifest.json") != self._manifest:
                raise LearningLoopError("Learning manifest changed")
            yield
        finally:
            if acquired:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._mutex.release()

    def _text(self, relative):
        try:
            text = mutation.read_text(self.root / relative, vault_root=self.root)
            if text is not None and len(text.encode()) > MAX_RECORD_BYTES:
                raise LearningLoopError("Owned learning artifact exceeds bound")
            return text
        except (OSError, ValueError) as exc:
            raise LearningLoopError("Cannot read exact owned learning artifact") from exc

    def _write(self, relative, text):
        """Only create immutable artifacts, using the actual mutation ledger."""
        self._live_lock()
        expected = hashlib.sha256(text.encode()).hexdigest()
        op = mutation.operation_id("public-learning-loop-v1", self.run_id,
                                   self.arm_id, relative, expected)
        target = self.root / relative
        prior = self._text(relative)
        if prior is not None:
            key = mutation.operation_id(mutation.operation_id(relative), op)
            effect = mutation._metadata(self.root, "effects", key)
            if (prior != text or not effect or effect.get("status") != "applied" or
                    effect.get("revision") != expected):
                raise LearningLoopError("Existing artifact is not the exact owned effect")
            return expected

        def create(existing):
            if existing is not None:
                raise LearningLoopError("Learning artifact appeared concurrently")
            return text
        result = mutation.mutate(target, create, vault_root=self.root,
                                 operation_id=op, allow_protected=True)
        if (not result.applied or result.revision != expected or
                self._text(relative) != text):
            raise LearningLoopError("Learning mutation did not commit exact bytes")
        return expected

    def _save(self, relative, value):
        return self._write(relative, (_encoded({"payload": value,
                            "sha256": _digest(value)}) + b"\n").decode())

    def _read(self, relative):
        text = self._text(relative)
        if text is None:
            return None
        try:
            record = json.loads(text)
            if (type(record) is not dict or set(record) != {"payload", "sha256"} or
                    type(record["payload"]) is not dict or
                    record["sha256"] != _digest(record["payload"]) or
                    text.encode() != _encoded(record) + b"\n"):
                raise LearningLoopError("Altered learning record")
            return record["payload"]
        except (ValueError, TypeError, KeyError) as exc:
            raise LearningLoopError("Altered learning record") from exc

    def _context_scope(self, scope):
        result = dict(self.scope)
        for key, value in _scope(scope).items():
            if key in self.scope and self.scope[key] != value:
                raise LearningLoopError("Context changes owner scope")
            result[key] = value
        return result

    def _observations(self):
        directory = self.root / "observations"
        if directory.is_symlink():
            raise LearningLoopError("Linked observation directory")
        names = sorted(directory.glob("*.json")) if directory.exists() else []
        if len(names) > MAX_OBSERVATIONS:
            raise LearningLoopError("Observation count bound exceeded")
        records = []
        for sequence, path in enumerate(names):
            if path.name != f"{sequence:05d}.json":
                raise LearningLoopError("Noncontiguous observation journal")
            record = self._read("observations/" + path.name)
            if (record is None or record.get("run_id") != self.run_id or
                    record.get("arm_id") != self.arm_id or
                    record.get("sequence") != sequence):
                raise LearningLoopError("Observation ownership differs")
            records.append(record)
        return records

    @staticmethod
    def _receipt(observation):
        return {"kind": "observation", **{key: observation[key] for key in
                ("run_id", "arm_id", "sequence", "task_id", "source_revision", "source_spans")},
                "receipt_digest": _digest(observation)}

    def _check_origins(self, text):
        # Untrusted text cannot cause lineage traversal into another vault.
        try:
            for origin in egress.derivations(text):
                mutation.resolve_note_path(origin, self.root, allow_protected=True)
        except (OSError, ValueError) as exc:
            raise LearningLoopError("Experience contains foreign or malformed lineage") from exc

    def _verified(self, observation):
        relative = f"captures/{observation['sequence']:05d}.jsonl"
        raw = self._text(relative)
        if raw is None or hashlib.sha256(raw.encode()).hexdigest() != observation["source_revision"]:
            raise LearningLoopError("Captured experience changed")
        transcript = transcript_reader.read_transcript(self.root / relative,
                                                       captured_bytes=raw.encode())
        if [event.anchor for event in transcript.source_events] != observation["source_spans"]:
            raise LearningLoopError("Captured source anchors changed")
        rows = [json.loads(line) for line in raw.splitlines()]
        experience = observation["experience"]
        if (len(rows) != 3 or rows[0]["message"]["content"] != experience["task"] or
                rows[1]["message"]["content"] != experience["actual_response"] or
                json.loads(rows[2]["message"]["content"]) != experience["verifier_feedback"]):
            raise LearningLoopError("Observation differs from its captured bytes")
        return self.root / relative

    def observe(self, task_id, task, actual_response, verifier_feedback, *, scope=None):
        task_id = _identity(task_id)
        if (not isinstance(task, str) or not task or not isinstance(actual_response, str) or
                type(verifier_feedback) is not dict):
            raise LearningLoopError("Actual task, response and verifier object required")
        experience = json.loads(_encoded({"task": task, "actual_response": actual_response,
                                         "verifier_feedback": verifier_feedback}))
        if len(_encoded(experience)) > MAX_EXPERIENCE_BYTES:
            raise LearningLoopError("Captured experience exceeds byte bound")
        self._check_origins(task)
        self._check_origins(actual_response)
        context = self._context_scope(scope)
        with self._locked():
            records = self._observations()
            for record in records:
                if record["task_id"] == task_id:
                    if record["experience"] != experience or record["scope"] != context:
                        raise LearningLoopError("Task replay conflicts with original capture")
                    self._verified(record)
                    return self._receipt(record)
            if len(records) == MAX_OBSERVATIONS:
                raise LearningLoopError("Observation count bound exceeded")
            sequence = len(records)
            rows = [{"type": "user", "message": {"content": task}},
                    {"type": "assistant", "message": {"content": actual_response}},
                    {"type": "user", "message": {"content": _encoded(verifier_feedback).decode()}}]
            raw = b"".join(_encoded(row) + b"\n" for row in rows)
            relative = f"captures/{sequence:05d}.jsonl"
            self._write(relative, raw.decode())
            source = self.root / relative
            egress.capture_restrictions(source, origins=[str(source)],
                                        captured_text=raw.decode(), vault_root=self.root)
            transcript = transcript_reader.read_transcript(source, captured_bytes=raw)
            observation = {"run_id": self.run_id, "arm_id": self.arm_id,
                "sequence": sequence, "task_id": task_id, "scope": context,
                "experience": experience, "source_revision": hashlib.sha256(raw).hexdigest(),
                "source_spans": [event.anchor for event in transcript.source_events]}
            self._save(f"observations/{sequence:05d}.json", observation)
            return self._receipt(observation)

    def _entries(self, records, cutoff):
        active = {}
        for observation in records[:cutoff]:
            sequence = observation["sequence"]
            result = self._read(f"learning/{sequence:05d}.result.json")
            if result is None:
                continue
            if result.get("observation_receipt_digest") != _digest(observation):
                raise LearningLoopError("Learning receipt refers to a different capture")
            if result.get("status") != "completed":
                continue
            self._verified(observation)
            for entry in result["entries"]:
                raw = self._text(entry["relative_path"])
                if (raw is None or hashlib.sha256(raw.encode()).hexdigest() != entry["revision"] or
                        entry["run_id"] != self.run_id or entry["arm_id"] != self.arm_id or
                        entry["source_revision"] != observation["source_revision"] or
                        not set(entry["source_spans"]).issubset(observation["source_spans"])):
                    raise LearningLoopError("Learned revision or provenance changed")
                inputs = entry.get("input_revisions")
                if type(inputs) is not list or not inputs:
                    raise LearningLoopError("Learned revision lacks exact extraction inputs")
                for item in inputs:
                    content = self._text(item["relative_path"])
                    if content is None or hashlib.sha256(content.encode()).hexdigest() != item["revision"]:
                        raise LearningLoopError("An extraction input revision changed")
                scope_key = _digest(entry["scope"])
                for key in entry["supersedes"]:
                    active.pop((scope_key, key), None)
                active[(scope_key, entry["key"])] = entry
        return list(active.values())

    def _revalidate_inputs(self, records, observation, prior):
        """Recheck precisely the evidence exposed to the callback, without replay."""
        self._live_lock()
        current = self._observations()
        if current != records:
            raise LearningLoopError("Observation journal changed during extraction")
        self._verified(observation)
        actual = self._applicable(self._entries(current, observation["sequence"]), observation["scope"])
        if actual != prior:
            raise LearningLoopError("Supplied memory changed during extraction")

    @staticmethod
    def _applicable(entries, scope):
        return [entry for entry in entries
                if all(scope.get(key) == value for key, value in entry["scope"].items())]

    def _proposals(self, response, observation, prior):
        response = json.loads(_encoded(response))
        if (type(response) is not dict or set(response) != {"lessons"} or
                type(response["lessons"]) is not list or len(response["lessons"]) > MAX_LESSONS):
            raise LearningLoopError("Invalid extraction schema")
        lessons, seen = [], set()
        known = {entry["key"] for entry in prior}
        for proposal in response["lessons"]:
            if (type(proposal) is not dict or set(proposal) -
                    {"key", "text", "source_spans", "scope", "supersedes"}):
                raise LearningLoopError("Extraction cannot grant authority or unknown fields")
            key = _identity(proposal.get("key"))
            text, spans = proposal.get("text"), proposal.get("source_spans")
            if (key in seen or not isinstance(text, str) or not text.strip() or
                    len(text) > MAX_LESSON_CHARS or type(spans) is not list or not spans or
                    any(not isinstance(span, str) for span in spans) or
                    not set(spans).issubset(observation["source_spans"])):
                raise LearningLoopError("Extraction lacks unique source-supported lessons")
            self._check_origins(text)
            constraints = _scope(proposal.get("scope"))
            if any(observation["scope"].get(k) != v for k, v in constraints.items()):
                raise LearningLoopError("Extraction changes observation scope")
            supersedes = proposal.get("supersedes", [])
            if (type(supersedes) is not list or len(supersedes) > MAX_LESSONS or
                    any(not isinstance(k, str) or k not in known for k in supersedes) or
                    len(set(supersedes)) != len(supersedes)):
                raise LearningLoopError("Supersession lacks an earlier own scoped lesson")
            seen.add(key)
            lessons.append({"key": key, "text": text, "source_spans": sorted(set(spans)),
                            "scope": dict(observation["scope"]), "supersedes": supersedes})
        if any(k in seen and k != item["key"] for item in lessons for k in item["supersedes"]):
            raise LearningLoopError("Ambiguous simultaneous supersession")
        return lessons

    def learn(self, observation_receipt):
        with self._locked():
            records = self._observations()
            if type(observation_receipt) is not dict:
                raise LearningLoopError("Exact observation receipt required")
            sequence = observation_receipt.get("sequence")
            if type(sequence) is not int or not 0 <= sequence < len(records):
                raise LearningLoopError("Observation is absent or future")
            observation = records[sequence]
            if observation_receipt != self._receipt(observation):
                raise LearningLoopError("Observation receipt differs from owned capture")
            source = self._verified(observation)
            prefix = f"learning/{sequence:05d}"
            previous = self._read(prefix + ".result.json")
            if previous is not None:
                if previous["observation_receipt_digest"] != _digest(observation):
                    raise LearningLoopError("Learning replay differs from original observation")
                self._entries(records, sequence + 1)
                return {key: value for key, value in previous.items() if key != "entries"}
            if self._read(prefix + ".intent.json") is not None:
                raise LearningLoopError("Unresolved learning intent; no automatic callback repeat")
            # Never process an old observation after later memory has committed.
            if any(self._read(f"learning/{n:05d}.result.json") is not None
                   for n in range(sequence + 1, len(records))):
                raise LearningLoopError("Cannot learn an earlier observation after a later one")
            prior = self._applicable(self._entries(records, sequence), observation["scope"])
            input_revisions = [{"relative_path": source.relative_to(self.root).as_posix(),
                                "revision": observation["source_revision"]},
                               *({"relative_path": entry["relative_path"], "revision": entry["revision"]}
                                 for entry in prior)]
            request = {"run_id": self.run_id, "arm_id": self.arm_id,
                "task_id": observation["task_id"], "sequence": sequence,
                "scope": observation["scope"], "source_revision": observation["source_revision"],
                "source_spans": observation["source_spans"], "experience": observation["experience"],
                "current_memory": prior}
            self._save(prefix + ".intent.json", {"run_id": self.run_id, "arm_id": self.arm_id,
                       "observation_receipt_digest": _digest(observation), "request_digest": _digest(request),
                       "input_revisions": input_revisions})
            outcome = {"kind": "learning", "run_id": self.run_id, "arm_id": self.arm_id,
                "sequence": sequence, "status": "failed", "created": [], "updated": [],
                "superseded": [], "reason": "extraction_failed", "entries": [],
                "observation_receipt_digest": _digest(observation)}
            try:
                sources = [str(self.root / item["relative_path"]) for item in input_revisions]
                rules = {"default": ["host", "local"], "rules": [{"source": str(self.root) + "/*",
                         "audiences": ["host", "local", "model"]}]}
                prompt = _encoded(request).decode()
                # Public opt-in grants only this owned root. Authored restrictions
                # and transitive lineage still fail closed; no private config read.
                if egress.prepare(prompt, audience="model", sources=sources, rules=rules) != prompt:
                    raise LearningLoopError("Public experience requires secret redaction")
                response = self.extractor(_freeze(json.loads(_encoded(request))))
                self._revalidate_inputs(records, observation, prior)
                lessons = self._proposals(response, observation, prior)
                known = {entry["key"] for entry in prior}
                entries = []
                for number, lesson in enumerate(lessons):
                    metadata = {"source_revision": observation["source_revision"],
                        "source_spans": lesson["source_spans"], "derivation_sources": sources,
                        "input_revisions": input_revisions, "scope": lesson["scope"]}
                    content = (mutation.serialize_frontmatter({"type": "gotcha", "verification": "proposed",
                        "derivation_sources": sources, "source_revision": observation["source_revision"]})
                        + "\n\n# " + lesson["key"] + "\n\n" + lesson["text"]
                        + "\n\n<!-- claude-note:provenance " + json.dumps(metadata, ensure_ascii=False).replace("-->", "\\u002d\\u002d>") + " -->\n")
                    relative = f"memory/{sequence:05d}-{number:02d}-{lesson['key']}.md"
                    revision = self._write(relative, content)
                    egress.capture_restrictions(self.root / relative, origins=sources,
                        captured_text=content, vault_root=self.root)
                    entries.append({**lesson, "run_id": self.run_id, "arm_id": self.arm_id,
                        "sequence": sequence, "source_revision": observation["source_revision"],
                        "revision": revision, "relative_path": relative, "verification": "proposed",
                        "input_revisions": input_revisions,
                        "observation_receipt_digest": _digest(observation)})
                self._revalidate_inputs(records, observation, prior)
                outcome.update(status="completed", reason="", entries=entries,
                    created=[e["key"] for e in lessons if e["key"] not in known],
                    updated=[e["key"] for e in lessons if e["key"] in known],
                    superseded=sorted({key for e in lessons for key in e["supersedes"]}))
            except Exception as error:
                # Keep fixed diagnostic types, partial files and admitted intent.
                outcome["reason"] = "extraction_or_mutation:" + type(error).__name__
            self._save(prefix + ".result.json", outcome)
            return {key: value for key, value in outcome.items() if key != "entries"}

    def retrieve(self, task, *, before_sequence=None, scope=None):
        if not isinstance(task, str) or len(task.encode()) > MAX_EXPERIENCE_BYTES:
            raise LearningLoopError("Bounded retrieval task required")
        context_scope = self._context_scope(scope)
        with self._locked():
            records = self._observations()
            cutoff = len(records) if before_sequence is None else before_sequence
            if type(cutoff) is not int or not 0 <= cutoff <= len(records):
                raise LearningLoopError("Retrieval cutoff is absent or future")
            candidates = self._applicable(self._entries(records, cutoff), context_scope)
            terms = set(re.findall(r"\w+", task.casefold()))
            candidates.sort(key=lambda e: (-len(terms & set(re.findall(r"\w+", (e["key"] + " " + e["text"]).casefold()))),
                                            -e["sequence"], e["key"]))
            selected, parts, size = [], [], 0
            for entry in candidates:
                part = "[" + entry["key"] + "; proposed] " + entry["text"]
                added = len(part) + (2 if parts else 0)
                if size + added <= self.max_memory_chars:
                    selected.append(json.loads(_encoded(entry)))
                    parts.append(part)
                    size += added
            context = "\n\n".join(parts)
            receipt = {"run_id": self.run_id, "arm_id": self.arm_id, "before_sequence": cutoff,
                       "scope": context_scope, "entries": [e["revision"] for e in selected],
                       "context_sha256": hashlib.sha256(context.encode()).hexdigest()}
            return {"context": context, "entries": selected, "characters": len(context),
                    "utf8_bytes": len(context.encode()), "receipt": {**receipt, "digest": _digest(receipt)}}

    def raw_history(self, *, max_chars=None):
        if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
            raise LearningLoopError("Invalid raw history character bound")
        with self._locked():
            records = self._observations()
            parts = []
            for observation in records:
                self._verified(observation)
                parts.append(_encoded({"task_id": observation["task_id"],
                    "sequence": observation["sequence"], "scope": observation["scope"],
                    **observation["experience"]}).decode())
            text = "\n".join(parts)
            return text if max_chars is None else text[-max_chars:] if max_chars else ""
