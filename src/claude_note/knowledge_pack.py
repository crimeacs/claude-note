"""
Knowledge Pack schema for claude-note synthesizer.

Defines the structured output format for knowledge extraction.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional


NOTE_TYPES = (
    "pattern", "gotcha", "decision", "reference", "project", "log",
    "literature", "source", "feedback", "meta", "session",
)


@dataclass
class Concept:
    """A concept or topic learned during the session."""
    name: str
    summary: str                           # 2-4 sentences
    tags: list[str] = field(default_factory=list)
    links_suggested: list[str] = field(default_factory=list)  # Note names to link to

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Concept":
        return cls(**data)


@dataclass
class Decision:
    """A decision made during the session."""
    decision: str                          # What was decided
    rationale: str                         # Why
    evidence: list[str] = field(default_factory=list)  # Supporting facts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(**data)


@dataclass
class OpenQuestion:
    """An open question identified during the session."""
    question: str
    context: str                           # Why this question matters
    suggested_next_step: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OpenQuestion":
        return cls(**data)


@dataclass
class HowTo:
    """A procedure or how-to learned during the session."""
    title: str
    steps: list[str] = field(default_factory=list)
    gotchas: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HowTo":
        return cls(**data)


@dataclass
class NoteOp:
    """An operation to perform on a note."""
    op: str                                # "create" | "upsert_block" | "append"
    path: str                              # Note filename
    body_markdown: str                     # Content to write
    frontmatter: Optional[dict] = None     # For create
    managed_block_id: Optional[str] = None # For upsert_block
    section: Optional[str] = None          # For append (e.g., "## Synthesized")

    def to_dict(self) -> dict:
        d = asdict(self)
        # Remove None values
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "NoteOp":
        return cls(**data)


@dataclass
class KnowledgePack:
    """Complete knowledge extraction from a session."""
    session_id: str
    date: str                              # ISO date (YYYY-MM-DD)
    title: str                             # Human-readable session title
    time: str = ""                         # Time (HH:MM:SS), optional
    highlights: list[str] = field(default_factory=list)  # 1-3 key outcomes
    concepts: list[Concept] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    howtos: list[HowTo] = field(default_factory=list)
    note_ops: list[NoteOp] = field(default_factory=list)
    assistant: str = ""                    # provenance.ASSISTANTS; set by us, not the model

    def to_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "date": self.date,
            "title": self.title,
            "highlights": self.highlights,
            "concepts": [c.to_dict() for c in self.concepts],
            "decisions": [d.to_dict() for d in self.decisions],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "howtos": [h.to_dict() for h in self.howtos],
            "note_ops": [op.to_dict() for op in self.note_ops],
        }
        if self.time:
            d["time"] = self.time
        if self.assistant:
            d["assistant"] = self.assistant
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgePack":
        return cls(
            session_id=data.get("session_id", ""),
            date=data.get("date", ""),
            title=data.get("title", ""),
            time=data.get("time", ""),
            highlights=data.get("highlights", []),
            concepts=[Concept.from_dict(c) for c in data.get("concepts", [])],
            decisions=[Decision.from_dict(d) for d in data.get("decisions", [])],
            open_questions=[OpenQuestion.from_dict(q) for q in data.get("open_questions", [])],
            howtos=[HowTo.from_dict(h) for h in data.get("howtos", [])],
            note_ops=[NoteOp.from_dict(op) for op in data.get("note_ops", [])],
            assistant=data.get("assistant", ""),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "KnowledgePack":
        data = json.loads(json_str)
        return cls.from_dict(data)

    def is_empty(self) -> bool:
        """Check if pack has any content worth saving."""
        return (
            not self.highlights
            and not self.concepts
            and not self.decisions
            and not self.open_questions
            and not self.howtos
            and not self.note_ops
        )


def get_schema_description() -> str:
    """
    Get a human-readable description of the KnowledgePack schema.

    Used in synthesis prompts.
    """
    return """
{
    "session_id": "string - session identifier",
    "date": "string - ISO date (YYYY-MM-DD)",
    "time": "string - time (HH:MM:SS), optional",
    "title": "string - human-readable session title (5-10 words)",
    "highlights": ["string - 1-3 key outcomes of the session"],
    "concepts": [
        {
            "name": "string - concept name",
            "summary": "string - 2-4 sentence explanation",
            "tags": ["string - relevant tags from vault"],
            "links_suggested": ["string - note names to link to"]
        }
    ],
    "decisions": [
        {
            "decision": "string - what was decided",
            "rationale": "string - why this decision",
            "evidence": ["string - supporting facts"]
        }
    ],
    "open_questions": [
        {
            "question": "string - the question",
            "context": "string - why this matters",
            "suggested_next_step": "string - how to investigate"
        }
    ],
    "howtos": [
        {
            "title": "string - procedure title",
            "steps": ["string - step-by-step instructions"],
            "gotchas": ["string - pitfalls to avoid"]
        }
    ],
    "note_ops": [
        {
            "op": "create | upsert_block | append",
            "path": "string - note filename (e.g., 'my-note.md')",
            "frontmatter": {"type": "pattern | gotcha | decision | reference | project | log | literature | source | feedback | meta | session", "tags": [...], ...},  // for create only; choose exactly one type explicitly
            "body_markdown": "string - content to write",
            "managed_block_id": "string",  // for upsert_block only
            "section": "string"  // for append only (e.g., '## Synthesized')
        }
    ]
}
""".strip()


def validate_knowledge_pack(pack: KnowledgePack) -> list[str]:
    """
    Validate a knowledge pack for common issues.

    Returns list of warning messages (empty if valid).
    """
    warnings = []

    # Check required fields
    if not pack.session_id:
        warnings.append("Missing session_id")
    if not pack.date:
        warnings.append("Missing date")
    if not pack.title:
        warnings.append("Missing title")

    # Check concepts
    for i, concept in enumerate(pack.concepts):
        if not concept.name:
            warnings.append(f"Concept {i}: missing name")
        if not concept.summary:
            warnings.append(f"Concept {i}: missing summary")
        if len(concept.summary) > 500:
            warnings.append(f"Concept {i}: summary too long (>500 chars)")

    # Check note_ops
    for i, op in enumerate(pack.note_ops):
        if op.op not in ("create", "upsert_block", "append"):
            warnings.append(f"NoteOp {i}: invalid op '{op.op}'")
        if not op.path:
            warnings.append(f"NoteOp {i}: missing path")
        if op.op == "create" and not op.frontmatter:
            warnings.append(f"NoteOp {i}: create op missing frontmatter")
        if op.op == "create":
            note_type = (op.frontmatter or {}).get("type")
            if note_type not in NOTE_TYPES:
                warnings.append(f"NoteOp {i}: missing or invalid type {note_type!r}; choose an explicit note type")
        if op.op == "upsert_block" and not op.managed_block_id:
            warnings.append(f"NoteOp {i}: upsert_block op missing managed_block_id")

    return warnings
