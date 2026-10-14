"""
Research ingestion module for claude-note.

Supports two modes:
- Literature (default): External research (papers, reviews, docs) -> `lit-*` notes in `literature/`
- Internal: Team docs, processes, architecture -> `int-*` notes in `internal/`
"""

import itertools
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import qmd_search
from . import egress
from .document_sections import sections
from .state_io import atomic_json, read_json


# =============================================================================
# Progress Spinner
# =============================================================================

class Spinner:
    """Simple terminal spinner for long-running operations."""

    def __init__(self, message: str = "Processing"):
        self.message = message
        self.running = False
        self.thread = None
        self.start_time = None

    def _spin(self):
        chars = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
        while self.running:
            elapsed = int(time.time() - self.start_time)
            sys.stdout.write(f"\r{next(chars)} {self.message} ({elapsed}s)")
            sys.stdout.flush()
            time.sleep(0.1)

    def __enter__(self):
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        elapsed = int(time.time() - self.start_time)
        sys.stdout.write(f"\r✓ {self.message} ({elapsed}s)\n")
        sys.stdout.flush()


# =============================================================================
# Document Conversion
# =============================================================================

def convert_to_text(file_path: Path) -> str:
    """
    Convert a document to plain text.

    Supports: .docx, .pdf, .md, .txt
    """
    suffix = file_path.suffix.lower()

    if suffix in ['.txt', '.md']:
        return file_path.read_text()

    elif suffix == '.docx':
        # Use pandoc
        result = subprocess.run(
            ['pandoc', str(file_path), '-t', 'plain', '--wrap=none'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pandoc failed: {result.stderr}")
        return result.stdout

    elif suffix == '.pdf':
        # Try pymupdf (fitz) first - best quality
        try:
            import fitz  # pymupdf
            doc = fitz.open(str(file_path))
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
            if text_parts:
                text = "\n\n".join(text_parts)
                # Remove null bytes that can appear in PDF extraction
                text = text.replace('\x00', '')
                if text.strip():
                    return text
        except ImportError:
            pass
        except Exception:
            pass  # Fall through to other methods

        # Fallback: try pdftotext
        try:
            result = subprocess.run(
                ['pdftotext', '-layout', str(file_path), '-'],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.replace('\x00', '')
        except FileNotFoundError:
            pass

        # Last resort: try pandoc
        try:
            result = subprocess.run(
                ['pandoc', str(file_path), '-t', 'plain', '--wrap=none'],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.replace('\x00', '')
        except FileNotFoundError:
            pass

        raise RuntimeError(
            "Could not convert PDF. Install one of: pymupdf (pip install pymupdf), "
            "pdftotext (brew install poppler), or pandoc with PDF support."
        )

    else:
        raise ValueError(f"Unsupported file type: {suffix}")


# =============================================================================
# Extraction Prompt
# =============================================================================

LITERATURE_EXTRACTION_PROMPT = '''You are extracting knowledge from an external research document (paper, review, report) for a personal knowledge base.

## Document
Title: {title}
Source file: {filename}

## Content (may be truncated)
{content}

## Context
This knowledge base spans multiple projects. Use the document's own context;
never invent a project affiliation, customer, owner, or result.

## Your Task
Extract knowledge into atomic notes. Each note should be a single concept, finding, or technique that could be useful.

Return this exact JSON schema:
{{
  "source_summary": "2-3 sentence summary of what this document covers",
  "source_type": "paper|review|report|documentation|other",
  "key_citation": "Author et al. (Year) or document title for citation",
  "interesting_takeaways": "1-2 paragraph narrative summary of the most interesting, surprising, or actionable findings from this document. Write conversationally as if briefing a colleague. Focus on insights that could change how we think about or approach our work, specific numbers or results that stand out, and any 'aha moments' or counterintuitive findings. Skip generic observations.",
  "notes": [
    {{
      "slug": "short-kebab-case-name",
      "title": "Human Readable Title",
      "type": "finding|technique|definition|benchmark|open-question",
      "summary": "2-4 sentence explanation of this concept",
      "details": "Optional longer explanation with specifics, numbers, quotes",
      "relevance": "Practical implications supported by the document (or null)",
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

## Rules
1. Extract only meaningful source-backed concepts; an empty notes array is valid when nothing durable is present
2. Each note should stand alone - someone reading just that note should understand it
3. Use `relevance` for implications supported by the source; separate findings from inference
4. For findings, include specific numbers/results when available
5. Slugs become filenames: "evaluation-method" -> "lit-evaluation-method.md"; use only lowercase letters, numbers and single hyphens
6. Tags should describe topics actually present in this document
7. Skip generic/obvious information - focus on actionable insights
8. If the document has explicit "key findings" or "recommendations", extract those

Return ONLY valid JSON. No markdown, no explanation.
'''

INTERNAL_EXTRACTION_PROMPT = '''You are extracting institutional knowledge from an internal team document for a personal knowledge base.

## Document
Title: {title}
Source file: {filename}

## Content (may be truncated)
{content}

## Context
This knowledge base spans multiple teams and projects. Internal docs may cover:
- Development processes and workflows
- Architecture decisions and patterns
- Team conventions and standards
- How-to guides and runbooks
- Code organization and ownership
- Historical decisions and their rationale

## Your Task
Extract knowledge into atomic notes. Focus on institutional knowledge that helps someone understand HOW things work here, WHY decisions were made, and WHAT conventions to follow.

Return this exact JSON schema:
{{
  "source_summary": "2-3 sentence summary of what this document covers",
  "source_type": "process|architecture|decision|convention|reference|how-to",
  "key_citation": "Document title or identifier",
  "interesting_takeaways": "1-2 paragraph narrative summary of the most useful or important institutional knowledge from this document. Write conversationally as if briefing a new team member. Focus on non-obvious processes, key decisions and their rationale, gotchas to watch out for, and anything that would save someone time or prevent mistakes.",
  "notes": [
    {{
      "slug": "short-kebab-case-name",
      "title": "Human Readable Title",
      "type": "process|architecture|decision|convention|reference|how-to",
      "summary": "2-4 sentence explanation of this concept",
      "details": "Optional longer explanation with specifics, steps, examples",
      "owner": "Team or person responsible (or null if unknown)",
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

## Rules
1. Extract only meaningful source-backed concepts; an empty notes array is valid when nothing durable is present
2. Each note should stand alone - someone reading just that note should understand it
3. For processes, include the key steps and any gotchas
4. For decisions, capture the WHY (rationale) not just the WHAT
5. For conventions, be explicit about dos and don'ts
6. Slugs become filenames: "deploy-process" -> "int-deploy-process.md"; use only lowercase letters, numbers and single hyphens
7. Tags should use existing conventions: #process, #architecture, #convention, #how-to, etc.
8. Skip generic/obvious information - focus on source-backed institutional knowledge
9. Preserve the source's project boundaries; never invent an affiliation or owner

Return ONLY valid JSON. No markdown, no explanation.
'''


# =============================================================================
# Knowledge Extraction
# =============================================================================

def _extract_section_knowledge(
    content: str,
    title: str,
    filename: str,
    model: str = None,
    timeout: int = 180,
    mode: str = "literature",
    source_path: str = "",
) -> dict:
    """
    Call Claude CLI to extract structured knowledge from document content.

    Args:
        mode: "literature" (default) or "internal"
    """
    # config imported at module level

    if model is None:
        model = getattr(config, "SYNTH_MODEL", "claude-sonnet-4-5-20250929")

    if len(content) > 25000:
        raise ValueError("Extraction requires bounded source sections")

    # Select prompt based on mode
    prompt_template = INTERNAL_EXTRACTION_PROMPT if mode == "internal" else LITERATURE_EXTRACTION_PROMPT
    prompt = prompt_template.format(
        title=title,
        filename=filename,
        content=content,
    )

    # Disable hooks to prevent recursion
    env = egress.model_env()
    prompt = egress.prepare(prompt, audience="model", sources=[source_path or filename])

    with tempfile.TemporaryDirectory(prefix='claude-note-model-') as work:
        result = subprocess.run(egress.model_command('claude', prompt, model=model),
                                capture_output=True, text=True, env=env,
                                timeout=timeout, cwd=work)

    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed: {result.stderr}")

    # Parse JSON from output
    output = result.stdout.strip()
    # Remove any null bytes that might have been passed through
    output = output.replace('\x00', '')

    # Remove markdown code blocks if present
    if output.startswith("```"):
        first_newline = output.find("\n")
        if first_newline > 0:
            output = output[first_newline + 1:]
        if output.endswith("```"):
            output = output[:-3].strip()

    # Find JSON object
    json_match = re.search(r'\{[\s\S]*\}', output)
    if json_match:
        output = json_match.group()

    try:
        value = json.loads(output)
        if not isinstance(value, dict) or not isinstance(value.get("notes", []), list):
            raise ValueError("Extraction must be an object with a notes list")
        if len(value.get("notes", [])) > 200:
            raise ValueError("Too many concepts in a source section")
        return value
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse extraction result: {e}\nOutput: {output[:500]}")


def extract_knowledge(content, title, filename, model=None, timeout=180, mode="literature", source_path=""):
    """Extract every section, caching expensive work before downstream mutation.

    Repeated commands resume cached sections. A missed deadline raises instead
    of publishing a prefix as a complete source. Equal sections reuse work;
    source offsets/revisions are assigned after cache loading, never by a model.
    """
    parts = sections(content)
    deadline = time.monotonic() + timeout
    extracted = []
    for part in parts:
        key = hashlib.sha256(json.dumps(["sections-v1", part["revision"], title, filename, source_path,
                model or config.SYNTH_MODEL, mode], sort_keys=True).encode()).hexdigest()
        cache = config.STATE_DIR / "documents" / (key + ".json")
        try:
            value = read_json(cache)
        except (OSError, ValueError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Document extraction deadline; cached sections retained, run again to resume")
            value = _extract_section_knowledge(part["text"], title, filename, model,
                                               timeout=remaining, mode=mode, source_path=source_path)
            atomic_json(cache, value)
        for note in value.get("notes", []):
            if not isinstance(note, dict) or not isinstance(note.get("summary", ""), str):
                raise ValueError("Invalid extracted concept")
            note["source_spans"] = [part["anchor"]]
            note["source_revision"] = part["source_revision"]
            note["verification"] = "proposed"
        extracted.append(value)
    result = dict(extracted[0])
    # Preserve distinct findings, including contradictions. Similarity is only
    # a link suggestion; it cannot erase a later source qualification.
    result["notes"] = [note for value in extracted for note in value.get("notes", [])]
    result["source_summary"] = "\n\n".join(value.get("source_summary", "") for value in extracted)
    result["interesting_takeaways"] = "\n\n".join(value.get("interesting_takeaways", "") for value in extracted)
    result["source_revision"] = parts[0]["source_revision"]
    result["coverage"] = {"characters": len(content), "covered_characters": sum(p["end"]-p["start"] for p in parts),
                          "sections": [{k: v for k, v in p.items() if k != "text"} for p in parts], "complete": True}
    return result


# =============================================================================
# Semantic Deduplication and Merge
# =============================================================================

def _find_similar_existing_concept(
    concept: dict,
    output_dir: Path,
    min_score: float = 0.75,
) -> Optional[Path]:
    """
    Check if a semantically similar concept note already exists.

    Uses qmd vector search to find notes that cover the same concept
    even if they use different wording or titles.

    Args:
        concept: Concept dict with name, summary, etc.
        output_dir: Directory to search in
        min_score: Minimum similarity threshold

    Returns:
        Path to similar existing note, or None if not found
    """
    # config imported at module level

    # Check if deduplication is enabled
    collection = getattr(config, "QMD_COLLECTION", "")
    if not config.QMD_SYNTH_ENABLED or not getattr(config, "QMD_INGEST_DEDUP_ENABLED", True) or not collection:
        return None

    try:
        # qmd_search imported at module level

        if not qmd_search.is_qmd_available():
            return None

        # Build query from concept name and summary
        query_parts = []
        if concept.get("title"):
            query_parts.append(concept["title"])
        if concept.get("summary"):
            query_parts.append(concept["summary"][:200])
        if concept.get("slug"):
            query_parts.append(concept["slug"].replace("-", " "))

        if not query_parts:
            return None

        query = " ".join(query_parts)
        threshold = getattr(config, "QMD_INGEST_DEDUP_THRESHOLD", min_score)

        # Search for similar content
        results = qmd_search.search_vector(query, limit=3, min_score=threshold, collection=collection)

        if not results:
            return None

        # Check if any result is in the output directory
        for result in results:
            result_path = qmd_search.resolve_result_path(result.path, config.VAULT_ROOT, collection)
            if (result_path and result_path.is_file()
                    and result_path.parent.resolve() == output_dir.resolve()
                    and not result_path.is_symlink()):
                # Source indexes are not atomic concepts, even if similar.
                if not re.search(r"^source_file:", result_path.read_text(encoding="utf-8"), re.MULTILINE):
                    return result_path

        return None

    except Exception:
        # Silent fallback - don't break ingestion
        return None


MERGE_ASSESSMENT_PROMPT = '''You are comparing a new concept extraction against an existing note to determine if merging is worthwhile.

## Existing Note Content
{existing_content}

## New Concept from "{new_source}"
**Title:** {new_title}
**Summary:** {new_summary}
**Details:** {new_details}
**Relevance:** {new_relevance}

## Your Task
Assess whether the new source adds genuinely new information that's worth appending.

Consider:
- Does it provide new techniques, numbers, or findings not in the existing note?
- Does it offer a different perspective or application?
- Is it just restating the same concept with different words?

Return this exact JSON schema:
{{
  "has_new_info": true or false,
  "new_info_summary": "2-4 sentence summary of what's new (or null if nothing new)",
  "reasoning": "Brief explanation of your assessment"
}}

Return ONLY valid JSON. No markdown, no explanation.
'''


def _source_metadata(value):
    """Every extraction remains proposed and carries its real source anchors."""
    spans = value.get('source_spans', [])
    if not spans and isinstance(value.get('coverage'), dict):
        spans = [part['anchor'] for part in value['coverage'].get('sections', []) if part.get('anchor')]
    return {'verification': 'proposed', 'source_revision': value.get('source_revision', ''),
            'source_spans': spans, 'coverage': json.dumps(value.get('coverage', {}), sort_keys=True),
            'source_origin': value.get('source_origin', ''),
            'derivation_sources': value.get('derivation_sources', [])}


def _create_ingested_note(path, frontmatter, body, identity):
    from . import mutation
    content = mutation.serialize_frontmatter(frontmatter) + "\n\n" + body.rstrip() + "\n"
    return mutation.create_note(path, content, vault_root=config.VAULT_ROOT,
                                operation_id=identity).path


def _preserve_additional_source(existing_note, concept, citation, source_note, reason):
    """Keep source evidence immutable when a merge is capped, denied or uncertain."""
    from . import mutation, provenance
    identity = mutation.operation_id('additional-source', existing_note.name, citation, concept)
    path = existing_note.with_name(existing_note.stem[:75] + '-source-' + identity[:16] + '.md')
    source_link = _note_link(source_note) if source_note else citation
    fm = provenance.stamp({'type': 'source' if existing_note.name.startswith('int-') else 'literature',
                           'source': source_link, **_source_metadata(concept)})
    body = (f"# {concept.get('title', concept.get('slug', 'Additional source'))}\n\n"
            f"{concept.get('summary', '')}\n\n{concept.get('details', '')}\n\n"
            f"## Source\n\n{source_link} — {citation}\n\n"
            f"## Related concept\n\n{_note_link(existing_note)}\n\n"
            f"Preserved separately: {reason}.\n")
    _create_ingested_note(path, fm, body, identity)
    # Physical append leaves the existing concept's prose and correction intact.
    marker = '<!-- claude-note:additional-source:' + identity + ' -->'
    def link(old):
        if old is None or marker in old:
            return None
        return old + f"\n{marker}\nAdditional proposed evidence: {_note_link(path)}\n"
    result = mutation.mutate(existing_note, link, vault_root=config.VAULT_ROOT,
                             operation_id=mutation.operation_id('additional-source-link', identity))
    if not result.applied:
        print(f"  Related-source link staged for review: {result.staged_path}")
    print(f"  Preserved additional proposed source: {path.name} ({reason})")
    return path


def _merge_concept_sources(existing_note: Path, new_concept: dict, new_source_citation: str,
                           model: str = None, source_note: Path = None) -> Optional[Path]:
    """Assess a cross-source addition; never let an assessment discard evidence.

    New evidence is appended without rewriting the existing note. A cap, policy
    denial, failed assessment or uncertain equivalence creates a linked immutable
    proposed note instead. Existing human corrections remain authoritative text.
    """
    from . import mutation
    existing_content = mutation.read_text(existing_note, vault_root=config.VAULT_ROOT)
    if existing_content is None:
        raise FileNotFoundError(existing_note)
    identity = mutation.operation_id('concept-source', new_source_citation, new_concept)
    marker = '<!-- claude-note:concept-source:' + identity + ' -->'
    if marker in existing_content:
        return existing_note
    def preserve(reason):
        return _preserve_additional_source(existing_note, new_concept, new_source_citation, source_note, reason)
    if not getattr(config, 'INGEST_MERGE_ENABLED', True):
        return preserve('merge disabled')
    source_count = len(re.findall(r'^\s*-\s*"?\[\[', existing_content, re.MULTILINE))
    source_count += len(re.findall(r'^source:', existing_content, re.MULTILINE))
    source_count += existing_content.count('<!-- claude-note:concept-source:')
    if source_count >= getattr(config, 'INGEST_MAX_SOURCES_PER_CONCEPT', 5):
        return preserve('concept source limit reached')
    new_sources = list(new_concept.get('derivation_sources', []))
    if new_concept.get('source_origin'):
        new_sources.append(new_concept['source_origin'])
    if not new_sources and source_note is not None and source_note.is_file():
        new_sources.append(str(source_note))
    if not new_sources:
        return preserve('actual source path unavailable for model egress policy')
    if len(existing_content) > 24000:
        return preserve('existing note exceeds bounded merge assessment')
    prompt = MERGE_ASSESSMENT_PROMPT.format(existing_content=existing_content,
        new_source=new_source_citation, new_title=new_concept.get('title', ''),
        new_summary=new_concept.get('summary', ''), new_details=new_concept.get('details', ''),
        new_relevance=new_concept.get('relevance', ''))
    try:
        derivation_sources = sorted(set([str(existing_note),
            *egress.derivations(existing_content), *new_sources]))
        prompt = egress.prepare(prompt, audience='model', sources=derivation_sources)
        with tempfile.TemporaryDirectory(prefix='claude-note-model-') as work:
            result = subprocess.run(egress.model_command('claude', prompt, model=model or config.SYNTH_MODEL),
                                    capture_output=True, text=True, env=egress.model_env(), timeout=60, cwd=work)
        if result.returncode != 0:
            return preserve('merge assessment failed')
        match = re.search(r'\{[\s\S]*\}', result.stdout)
        assessment = json.loads(match.group() if match else result.stdout)
        summary = assessment.get('new_info_summary', '')
        if not assessment.get('has_new_info') or not isinstance(summary, str) or not summary.strip():
            return preserve('assessment did not establish a new addition')
    except egress.EgressDenied:
        return preserve('model egress not authorized for every source')
    except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
        return preserve('merge assessment unavailable or invalid')
    metadata = _source_metadata(new_concept)
    source_link = _note_link(source_note) if source_note else new_source_citation
    addition = (f"\n{marker}\n## Additional proposed source: {new_source_citation}\n\n"
                f"{summary}\n\n### Extracted evidence\n\n{new_concept.get('summary', '')}\n\n"
                f"{new_concept.get('details', '')}\n\nSource: {source_link}\n"
                f"Verification: proposed\nSource revision: {metadata['source_revision']}\n"
                f"Source spans: {json.dumps(metadata['source_spans'])}\n"
                + '<!-- claude-note:provenance ' + json.dumps({
                    'derivation_sources': derivation_sources, 'source_revision': metadata['source_revision'],
                    'source_spans': metadata['source_spans']}, sort_keys=True).replace('-->', '\\u002d\\u002d>') + ' -->\n')
    result = mutation.mutate(existing_note, lambda old: None if old is None or marker in old else old + addition,
                             vault_root=config.VAULT_ROOT, operation_id=identity)
    if not result.applied:
        print(f"  Concept addition staged for review: {result.staged_path}")
        return preserve('canonical addition requires review')
    return existing_note


# =============================================================================
# Note Creation
# =============================================================================

def slugify(text: str) -> str:
    """Convert text to kebab-case slug."""
    # Lowercase and replace spaces/underscores with hyphens
    slug = text.lower().strip()
    slug = re.sub(r'[_\s]+', '-', slug)
    # Remove non-alphanumeric except hyphens
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    # Collapse multiple hyphens
    slug = re.sub(r'-+', '-', slug)
    # Trim hyphens from ends
    slug = slug.strip('-')
    if not slug:
        import hashlib
        slug = "note-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return slug[:50]  # Limit length


def _concept_slug(concept: dict) -> str:
    """Validate the model's filename component before creating any notes."""
    slug = concept.get("slug", "")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError(f"Invalid concept slug: {slug!r}; expected lowercase kebab-case")
    return slug


def _source_path(extraction: dict, source_file: Path, output_dir: Path, mode: str) -> Path:
    slug = slugify(extraction.get("key_citation") or source_file.stem)
    prefix = "int" if mode == "internal" else "lit"
    return output_dir / f"{prefix}-{slug}.md"


def _note_link(note_path: Path) -> str:
    """Link to the real vault-relative file, including configured subfolders."""
    try:
        target = note_path.resolve().relative_to(config.VAULT_ROOT.resolve())
    except ValueError:
        target = Path(note_path.parent.name) / note_path.name
    return f"[[{target.with_suffix('').as_posix()}]]"


def _concept_path(concept: dict, output_dir: Path, mode: str, source_note: Path = None) -> Path:
    prefix = "int" if mode == "internal" else "lit"
    path = output_dir / f"{prefix}-{_concept_slug(concept)}.md"
    if source_note and path == source_note:
        path = path.with_name(f"{path.stem}-concept.md")
    return path


def create_source_note(
    extraction: dict,
    source_file: Path,
    output_dir: Path,
    date: str,
    mode: str = "literature",
    concept_paths: list[Path] = None,
) -> Path:
    """
    Create the main source/index note for the ingested document.

    Args:
        output_dir: Directory to write the note to
        mode: "literature" or "internal"
    """
    from . import managed_blocks, note_router, provenance, mutation

    note_path = _source_path(extraction, source_file, output_dir, mode)
    note_router.resolve_note_path(note_path, config.VAULT_ROOT)

    # Build note content based on mode
    if mode == "internal":
        tags = ["source/internal", f"int/{extraction.get('source_type', 'reference')}"]
    else:
        tags = ["source/literature", extraction.get("source_type", "paper")]

    # Collect all concept note links with folder prefix for proper resolution
    if concept_paths is None:
        concept_paths = [_concept_path(note, output_dir, mode, note_path) for note in extraction.get("notes", [])]
    concept_links = [_note_link(path) for path in dict.fromkeys(concept_paths) if path.is_file()]

    content = f"""# {extraction.get("key_citation", source_file.stem)}

{extraction.get("source_summary", "")}

## Extracted Concepts

{chr(10).join(f"- {link}" for link in concept_links) if concept_links else "(none)"}

## Source

- **File:** `{source_file.name}`
- **Ingested:** {date}
- **Type:** {extraction.get("source_type", "unknown")}
- **Verification:** proposed
- **Source revision:** {extraction.get("source_revision", "")}
- **Source spans:** {json.dumps(_source_metadata(extraction)["source_spans"])}
- **Coverage:** {json.dumps(extraction.get("coverage", {}), sort_keys=True)}

"""
    if note_path.exists():
        if note_path.is_symlink():
            raise ValueError(f"Source note is a symlink: {note_path}")
        existing = mutation.read_text(note_path, vault_root=config.VAULT_ROOT)
        source_match = re.search(r'^source_file:\s*(.+)$', existing.split("\n---", 1)[0], re.MULTILINE)
        source_value = source_match.group(1).strip() if source_match else ""
        if source_value.startswith('"'):
            try:
                source_value = json.loads(source_value)
            except json.JSONDecodeError:
                source_value = source_value.strip('"')
        else:
            source_value = source_value.strip("'")
        if source_value != source_file.name:
            raise FileExistsError(f"Source note path is already used by another note: {note_path}")
        def update(old):
            if old is None:
                raise FileNotFoundError(note_path)
            located = managed_blocks._locate(old, 'ingested-source')
            block = (managed_blocks._make_start_marker('ingested-source') + "\n" + content + "\n"
                     + managed_blocks._make_end_marker('ingested-source'))
            proposed = old[:located[0]] + block + old[located[1]:] if located else old + "\n" + block + "\n"
            # Refresh only our extraction metadata. All other frontmatter and
            # surrounding text remain intact in the proposal/owned-file update.
            match = re.match(r'^---\n(.*?)\n---', proposed, re.DOTALL)
            if match:
                fields = match.group(1)
                for key in _source_metadata(extraction):
                    fields = re.sub(r'^' + key + r':[^\n]*(?:\n[ \t]+[^\n]*)*\n?', '', fields, flags=re.MULTILINE)
                metadata = mutation.serialize_frontmatter(_source_metadata(extraction)).split('\n', 1)[1].rsplit('\n', 1)[0]
                proposed = '---\n' + fields.rstrip() + '\n' + metadata + '\n---' + proposed[match.end():]
            return proposed
        result = mutation.mutate(note_path, update, vault_root=config.VAULT_ROOT,
            expected_revision=mutation.revision(existing), require_revision=True,
            operation_id=mutation.operation_id('source-update', str(note_path), extraction, concept_links))
        if not result.applied:
            raise mutation.MutationConflict(result)
    else:
        fm = provenance.stamp({
            "type": "source" if mode == "internal" else "literature",
            "tags": tags, "source_file": source_file.name, "ingested": date,
            **_source_metadata(extraction),
        })
        # Keep marker syntax identical to the managed-block writer.
        block = managed_blocks._make_start_marker("ingested-source") + "\n" + content + "\n" + managed_blocks._make_end_marker("ingested-source")
        _create_ingested_note(note_path, fm, block, mutation.operation_id('source-note', str(note_path), extraction))
    return note_path


def create_concept_note(
    concept: dict,
    source_citation: str,
    output_dir: Path,
    date: str,
    mode: str = "literature",
    model: str = None,
    source_note: Path = None,
) -> Optional[Path]:
    """
    Create an atomic concept note from extracted knowledge.

    If a semantically similar note exists, attempts to merge the new
    source information into it instead of creating a duplicate.

    Args:
        output_dir: Directory to write the note to
        mode: "literature" or "internal"
        model: Optional model override for merge assessment

    Returns:
        Path to created/merged note, or None if skipped
    """
    if not concept.get("slug"):
        return None

    slug = _concept_slug(concept)
    prefix = "int" if mode == "internal" else "lit"
    note_path = _concept_path(concept, output_dir, mode, source_note)
    from . import note_router, provenance, mutation
    note_router.resolve_note_path(note_path, config.VAULT_ROOT)

    # Don't overwrite existing notes (exact match)
    if note_path.exists():
        # Try to merge sources into existing note
        merged = _merge_concept_sources(note_path, concept, source_citation, model=model, source_note=source_note)
        return merged  # Returns path if merged, None if nothing new

    # Check for semantically similar existing notes (fuzzy match)
    similar_note = _find_similar_existing_concept(concept, output_dir)
    if similar_note:
        # Try to merge instead of skipping
        merged = _merge_concept_sources(similar_note, concept, source_citation, model=model, source_note=source_note)
        return merged  # Returns path if merged, None if nothing new

    # Build tags based on mode
    folder = "internal" if mode == "internal" else "literature"
    if mode == "internal":
        tags = ["source/internal"]
        if concept.get("type"):
            tags.append(f"int/{concept['type']}")
    else:
        tags = ["source/literature"]
        if concept.get("type"):
            tags.append(f"lit/{concept['type']}")
    tags.extend(concept.get("tags", []))

    # Build content
    title = concept.get("title", slug.replace("-", " ").title())
    summary = concept.get("summary", "")
    details = concept.get("details", "")
    relevance = concept.get("relevance")
    owner = concept.get("owner")

    # Source link with folder prefix for proper resolution
    source_slug = slugify(source_citation)
    source_link = _note_link(source_note) if source_note else f"[[{folder}/{prefix}-{source_slug}]]"

    content = f"""# {title}

{summary}
"""

    if details:
        content += f"""
## Details

{details}
"""

    if relevance:
        content += f"""
## Relevance

{relevance}
"""

    if owner:
        content += f"""
## Owner

{owner}
"""

    content += f"\n## Source\n\n{source_link} — {source_citation}\n"
    fm = provenance.stamp({
        "type": "source" if mode == "internal" else "literature",
        "tags": tags, "source": source_link, "added": date,
        **_source_metadata(concept),
    })
    _create_ingested_note(note_path, fm, content, mutation.operation_id('concept-note', str(note_path), concept, source_citation))
    return note_path


# =============================================================================
# Main Ingestion Function
# =============================================================================

def ingest_document(
    file_path,
    title: str = None,
    model: str = None,
    dry_run: bool = False,
    mode: str = "literature",
) -> dict:
    """
    Ingest a research document into the vault.

    Args:
        file_path: Path to document (.docx, .pdf, .md, .txt)
        title: Optional title override (default: filename stem)
        model: Optional Claude model override
        dry_run: If True, extract but don't create notes
        mode: "literature" (default) or "internal"

    Returns:
        Dict with ingestion results
    """
    # config imported at module level

    # Determine output directory based on mode
    if mode == "internal":
        output_dir = config.INTERNAL_DIR
        prefix = "int"
    else:
        output_dir = config.LITERATURE_DIR
        prefix = "lit"

    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if title is None:
        title = file_path.stem.replace("-", " ").replace("_", " ")

    date = datetime.utcnow().strftime("%Y-%m-%d")

    # Convert to text
    with Spinner(f"Converting {file_path.name}"):
        content = convert_to_text(file_path)
    print(f"  {len(content):,} characters extracted")

    # Extract knowledge
    with Spinner(f"Extracting knowledge with Claude ({mode} mode)"):
        extraction = extract_knowledge(
            content=content,
            title=title,
            filename=file_path.name,
            model=model,
            mode=mode,
            source_path=str(file_path),
        )
    # Source identity is assigned by the importer, never by model output. It
    # accompanies every child so later egress can intersect the original input.
    extraction['source_origin'] = str(file_path)
    extraction['derivation_sources'] = [str(file_path)]
    for concept in extraction.get('notes', []):
        concept['source_origin'] = str(file_path)
        concept['derivation_sources'] = [str(file_path)]
    print(f"  {len(extraction.get('notes', []))} concepts found")
    for concept in extraction.get("notes", []):
        _concept_slug(concept)

    if dry_run:
        return {
            "dry_run": True,
            "mode": mode,
            "extraction": extraction,
            "source_note": None,
            "concept_notes": [],
        }

    # Create notes
    print(f"Creating notes in {output_dir.name}/...")

    source_citation = extraction.get("key_citation") or title

    # Reserve the source filename so a same-named concept cannot overwrite it.
    source_note = _source_path(extraction, file_path, output_dir, mode)

    # Create concept notes
    concept_notes = []
    merged_notes = []
    index_paths = []
    for concept in extraction.get("notes", []):
        expected_path = _concept_path(concept, output_dir, mode, source_note)
        already_existed = expected_path.exists()
        note_path = create_concept_note(concept, source_citation, output_dir, date, mode=mode,
                                        model=model, source_note=source_note)
        if note_path:
            index_paths.append(note_path)
            # Check if this was a merge (note already existed) vs new creation
            if note_path.name.startswith(expected_path.stem + '-source-'):
                concept_notes.append(note_path)
                print(f"  Created proposed source: {note_path.name}")
            elif already_existed or note_path != expected_path:
                merged_notes.append(note_path)
                print(f"  Merged into: {note_path.name}")
            else:
                concept_notes.append(note_path)
                print(f"  Created: {note_path.name}")
        else:
            if expected_path.is_file():
                index_paths.append(expected_path)
            slug = concept.get("slug", "?")
            print(f"  Skipped: {prefix}-{slug}.md (nothing new to add)")

    # Index only real output files; semantic merges may choose a different name.
    source_note = create_source_note(extraction, file_path, output_dir, date,
                                    mode=mode, concept_paths=index_paths)
    print(f"  Source: {source_note.name}")

    return {
        "dry_run": False,
        "mode": mode,
        "extraction": extraction,
        "source_note": source_note,
        "concept_notes": concept_notes,
        "merged_notes": merged_notes,
    }


# =============================================================================
# CLI Entry Point
# =============================================================================

def main(args) -> int:
    """CLI handler for ingest command."""
    try:
        # Determine mode from args
        mode = "internal" if getattr(args, "internal", False) else "literature"
        prefix = "int" if mode == "internal" else "lit"

        result = ingest_document(
            file_path=args.file,
            title=args.title,
            model=args.model,
            dry_run=args.dry_run,
            mode=mode,
        )

        if result["dry_run"]:
            print(f"\n=== Dry Run Results ({mode} mode) ===")
            ext = result["extraction"]
            print(f"Source: {ext.get('key_citation', 'Unknown')}")
            print(f"Type: {ext.get('source_type', 'Unknown')}")
            print(f"Summary: {ext.get('source_summary', 'N/A')}")
            print(f"\nWould create {len(ext.get('notes', []))} concept notes:")
            for note in ext.get("notes", []):
                print(f"  - {prefix}-{note.get('slug', '?')}.md: {note.get('title', '?')}")

            # Print interesting takeaways summary
            takeaways = ext.get("interesting_takeaways", "")
            if takeaways:
                print(f"\n{'─' * 60}")
                print("📌 Key Takeaways:")
                print(f"{'─' * 60}")
                print(takeaways)
                print(f"{'─' * 60}")
        else:
            print(f"\n=== Ingestion Complete ({mode} mode) ===")
            print(f"Source note: {result['source_note'].name}")
            print(f"Concept notes created: {len(result['concept_notes'])}")
            merged_count = len(result.get('merged_notes', []))
            if merged_count:
                print(f"Existing notes merged: {merged_count}")

            # Print interesting takeaways summary
            takeaways = result["extraction"].get("interesting_takeaways", "")
            if takeaways:
                print(f"\n{'─' * 60}")
                print("📌 Key Takeaways:")
                print(f"{'─' * 60}")
                print(takeaways)
                print(f"{'─' * 60}")

        return 0

    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except subprocess.TimeoutExpired:
        print("Error: Claude CLI timed out")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
