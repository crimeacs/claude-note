"""
Research ingestion module for claude-note.

Supports two modes:
- Literature (default): External research (papers, reviews, docs) -> `lit-*` notes in `literature/`
- Internal: Team docs, processes, architecture -> `int-*` notes in `internal/`
"""

import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import qmd_search


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
            if result.returncode == 0:
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
            if result.returncode == 0:
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
This knowledge base is for the "Fi" project - a dog activity tracking system with:
- Collar-mounted accelerometers (IMU)
- Kennelcam video for ground truth labeling
- ML behavior classification models
- Data pipelines (S3, Databricks, FiTag annotation platform)

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
      "relevance": "How this relates to Fi project (or null if general)",
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

## Rules
1. Create 3-15 atomic notes depending on document richness
2. Each note should stand alone - someone reading just that note should understand it
3. Use `relevance` to explicitly connect to Fi where applicable
4. For findings, include specific numbers/results when available
5. Slugs become filenames: "harness-vs-collar" -> "lit-harness-vs-collar.md"
6. Tags should use existing conventions: #ml, #sensors, #data-pipeline, #annotation, etc.
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
This knowledge base is for the "Fi" project - a dog activity tracking system. Internal docs may cover:
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
1. Create 3-15 atomic notes depending on document richness
2. Each note should stand alone - someone reading just that note should understand it
3. For processes, include the key steps and any gotchas
4. For decisions, capture the WHY (rationale) not just the WHAT
5. For conventions, be explicit about dos and don'ts
6. Slugs become filenames: "deploy-process" -> "int-deploy-process.md"
7. Tags should use existing conventions: #process, #architecture, #convention, #how-to, etc.
8. Skip generic/obvious information - focus on Fi-specific institutional knowledge

Return ONLY valid JSON. No markdown, no explanation.
'''


# =============================================================================
# Knowledge Extraction
# =============================================================================

def extract_knowledge(
    content: str,
    title: str,
    filename: str,
    model: str = None,
    timeout: int = 180,
    mode: str = "literature",
) -> dict:
    """
    Call Claude CLI to extract structured knowledge from document content.

    Args:
        mode: "literature" (default) or "internal"
    """
    # config imported at module level

    if model is None:
        model = getattr(config, "SYNTH_MODEL", "claude-sonnet-4-5-20250929")

    # Truncate content if too long (rough token estimate: 4 chars per token)
    max_chars = 100000  # ~25k tokens
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[... content truncated ...]"

    # Select prompt based on mode
    prompt_template = INTERNAL_EXTRACTION_PROMPT if mode == "internal" else LITERATURE_EXTRACTION_PROMPT
    prompt = prompt_template.format(
        title=title,
        filename=filename,
        content=content,
    )

    # Disable hooks to prevent recursion
    env = os.environ.copy()
    env["CLAUDE_CODE_HOOKS_ENABLED"] = "false"

    result = subprocess.run(
        ["claude", "-p", prompt, "--model", model],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )

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
        return json.loads(output)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse extraction result: {e}\nOutput: {output[:500]}")


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
    if not getattr(config, "QMD_INGEST_DEDUP_ENABLED", True):
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
        results = qmd_search.search_vector(query, limit=3, min_score=threshold)

        if not results:
            return None

        # Check if any result is in the output directory
        for result in results:
            result_path = Path(result.path)
            # Check both absolute and relative paths
            if output_dir.name in result.path:
                # Found a similar note in the same output directory
                return output_dir / result_path.name

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


def _merge_concept_sources(
    existing_note: Path,
    new_concept: dict,
    new_source_citation: str,
    model: str = None,
) -> Optional[Path]:
    """
    Merge new concept info into an existing similar note.

    Uses Claude to assess what's genuinely new and worth adding.
    Updates the note's YAML sources array and appends new findings.

    Args:
        existing_note: Path to the existing note to merge into
        new_concept: New concept dict with title, summary, details, relevance
        new_source_citation: Citation string for the new source
        model: Optional model override

    Returns:
        Path to merged note, or None if nothing new to add
    """
    # config imported at module level

    # Check if merge is enabled
    if not getattr(config, "INGEST_MERGE_ENABLED", True):
        return None

    if model is None:
        model = getattr(config, "SYNTH_MODEL", "claude-sonnet-4-5-20250929")

    # Read existing note
    existing_content = existing_note.read_text()

    # Parse YAML frontmatter to check source count
    yaml_match = re.match(r'^---\n(.*?)\n---', existing_content, re.DOTALL)
    if not yaml_match:
        return None  # Can't parse, skip

    yaml_content = yaml_match.group(1)

    # Count existing sources
    # Handle both single `source:` and array `sources:`
    sources_match = re.search(r'sources:\s*\n((?:\s+-\s*.*\n)*)', yaml_content)
    if sources_match:
        source_lines = [l.strip() for l in sources_match.group(1).strip().split('\n') if l.strip().startswith('-')]
        existing_source_count = len(source_lines)
    else:
        # Check for single source field
        single_source_match = re.search(r'^source:\s*"?(.+?)"?\s*$', yaml_content, re.MULTILINE)
        existing_source_count = 1 if single_source_match else 0

    max_sources = getattr(config, "INGEST_MAX_SOURCES_PER_CONCEPT", 5)
    if existing_source_count >= max_sources:
        print(f"  Max sources ({max_sources}) reached for '{existing_note.name}', skipping merge")
        return None

    # Check if this source is already present
    if new_source_citation in existing_content:
        print(f"  Source already present in '{existing_note.name}', skipping")
        return None

    # Call Claude to assess if there's new info worth adding
    prompt = MERGE_ASSESSMENT_PROMPT.format(
        existing_content=existing_content[:8000],  # Truncate if very long
        new_source=new_source_citation,
        new_title=new_concept.get("title", ""),
        new_summary=new_concept.get("summary", ""),
        new_details=new_concept.get("details", ""),
        new_relevance=new_concept.get("relevance", ""),
    )

    # Disable hooks to prevent recursion
    env = os.environ.copy()
    env["CLAUDE_CODE_HOOKS_ENABLED"] = "false"

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        if result.returncode != 0:
            print(f"  Merge assessment failed: {result.stderr[:100]}")
            return None

        # Parse response
        output = result.stdout.strip()
        if output.startswith("```"):
            first_newline = output.find("\n")
            if first_newline > 0:
                output = output[first_newline + 1:]
            if output.endswith("```"):
                output = output[:-3].strip()

        json_match = re.search(r'\{[\s\S]*\}', output)
        if json_match:
            output = json_match.group()

        assessment = json.loads(output)

        if not assessment.get("has_new_info", False):
            print(f"  Nothing new to add from '{new_source_citation}' to '{existing_note.name}'")
            return None

        new_info_summary = assessment.get("new_info_summary", "")
        if not new_info_summary:
            return None

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"  Merge assessment error: {e}")
        return None

    # Perform the merge
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Determine the prefix and folder for source link
    prefix = "int" if existing_note.name.startswith("int-") else "lit"
    folder = "internal" if prefix == "int" else "literature"
    source_slug = slugify(new_source_citation)
    new_source_link = f"[[{folder}/{prefix}-{source_slug}]]"

    # Update YAML frontmatter
    new_yaml = yaml_content

    # Convert single `source:` to `sources:` array if needed
    if sources_match:
        # Already has sources array, append
        new_source_line = f'  - "{new_source_link}"'
        # Insert before the last source line's newline
        insertion_point = sources_match.end(1)
        new_yaml = yaml_content[:insertion_point].rstrip() + f'\n{new_source_line}\n' + yaml_content[insertion_point:].lstrip('\n')
    elif single_source_match:
        # Convert to array format
        old_source = single_source_match.group(1).strip('"')
        new_sources_block = f'sources:\n  - "{old_source}"\n  - "{new_source_link}"'
        new_yaml = re.sub(r'^source:\s*"?.+?"?\s*$', new_sources_block, yaml_content, flags=re.MULTILINE)
    else:
        # No sources field, add one
        new_yaml = yaml_content.rstrip() + f'\nsources:\n  - "{new_source_link}"\n'

    # Add/update the `updated:` field
    if 'updated:' in new_yaml:
        new_yaml = re.sub(r'updated:\s*\S+', f'updated: {today}', new_yaml)
    else:
        new_yaml = new_yaml.rstrip() + f'\nupdated: {today}\n'

    # Rebuild frontmatter
    new_frontmatter = f"---\n{new_yaml.strip()}\n---"

    # Get content after frontmatter
    body = existing_content[yaml_match.end():].strip()

    # Check if "## Additional Sources" section exists
    additional_sources_match = re.search(r'^## Additional Sources\s*$', body, re.MULTILINE)

    # Build the new source entry
    new_entry = f"""
**From {new_source_citation}:**
{new_info_summary}
"""

    if additional_sources_match:
        # Append to existing section
        insert_pos = additional_sources_match.end()
        # Find the next section or end
        next_section = re.search(r'\n## ', body[insert_pos:])
        if next_section:
            insert_pos += next_section.start()
        else:
            insert_pos = len(body)
        body = body[:insert_pos].rstrip() + "\n" + new_entry + "\n" + body[insert_pos:].lstrip()
    else:
        # Add new section before the footer (if present) or at the end
        footer_match = re.search(r'\n---\s*\n\*Source:', body)
        if footer_match:
            body = body[:footer_match.start()] + "\n\n## Additional Sources\n" + new_entry + body[footer_match.start():]
        else:
            body = body.rstrip() + "\n\n## Additional Sources\n" + new_entry

    # Write updated content
    new_content = new_frontmatter + "\n\n" + body.strip() + "\n"
    existing_note.write_text(new_content)

    return existing_note


# =============================================================================
# Note Creation
# =============================================================================

def _author_line() -> str:
    """`author: <email>` plus newline for frontmatter, or "" when unknown."""
    from . import provenance
    author = provenance.author_email()
    return f"author: {author}\n" if author else ""


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
    return slug[:50]  # Limit length


def create_source_note(
    extraction: dict,
    source_file: Path,
    output_dir: Path,
    date: str,
    mode: str = "literature",
) -> Path:
    """
    Create the main source/index note for the ingested document.

    Args:
        output_dir: Directory to write the note to
        mode: "literature" or "internal"
    """
    slug = slugify(extraction.get("key_citation", source_file.stem))
    prefix = "int" if mode == "internal" else "lit"
    folder = "internal" if mode == "internal" else "literature"
    filename = f"{prefix}-{slug}.md"
    note_path = output_dir / filename

    # Build note content based on mode
    if mode == "internal":
        tags = ["source/internal", f"int/{extraction.get('source_type', 'reference')}", "project/fi"]
        concept_prefix = "int"
        related_links = [
            "[[fi-ml-meetings]] - Meeting index",
            "[[fi-ml-moc]] - ML project hub",
            "[[fi-moc]] - Fi project hub",
        ]
    else:
        tags = ["source/literature", extraction.get("source_type", "paper"), "project/fi"]
        concept_prefix = "lit"
        related_links = [
            "[[fi-ml-moc]] - ML project hub",
            "[[fi-moc]] - Fi project hub",
        ]

    # Collect all concept note links with folder prefix for proper resolution
    concept_links = []
    for note in extraction.get("notes", []):
        note_slug = note.get("slug", "")
        if note_slug:
            display_name = note.get("title", note_slug.replace("-", " ").title())
            concept_links.append(f"[[{folder}/{concept_prefix}-{note_slug}|{display_name}]]")

    content = f"""---
tags:
  - {tags[0]}
  - {tags[1]}
  - {tags[2]}
source_file: "{source_file.name}"
ingested: {date}
{_author_line()}---

# {extraction.get("key_citation", source_file.stem)}

{extraction.get("source_summary", "")}

## Extracted Concepts

{chr(10).join(f"- {link}" for link in concept_links) if concept_links else "(none)"}

## Source

- **File:** `{source_file.name}`
- **Ingested:** {date}
- **Type:** {extraction.get("source_type", "unknown")}

## Related

{chr(10).join(f"- {link}" for link in related_links)}
"""

    note_path.write_text(content)
    return note_path


def create_concept_note(
    concept: dict,
    source_citation: str,
    output_dir: Path,
    date: str,
    mode: str = "literature",
    model: str = None,
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
    slug = concept.get("slug", "")
    if not slug:
        return None

    prefix = "int" if mode == "internal" else "lit"
    filename = f"{prefix}-{slug}.md"
    note_path = output_dir / filename

    # Don't overwrite existing notes (exact match)
    if note_path.exists():
        # Try to merge sources into existing note
        merged = _merge_concept_sources(note_path, concept, source_citation, model=model)
        return merged  # Returns path if merged, None if nothing new

    # Check for semantically similar existing notes (fuzzy match)
    similar_note = _find_similar_existing_concept(concept, output_dir)
    if similar_note:
        # Try to merge instead of skipping
        merged = _merge_concept_sources(similar_note, concept, source_citation, model=model)
        return merged  # Returns path if merged, None if nothing new

    # Build tags based on mode
    folder = "internal" if mode == "internal" else "literature"
    if mode == "internal":
        tags = ["source/internal", "project/fi"]
        if concept.get("type"):
            tags.append(f"int/{concept['type']}")
        related_links = ["[[fi-ml-moc]]", "[[fi-moc]]"]
    else:
        tags = ["source/literature", "project/fi"]
        if concept.get("type"):
            tags.append(f"lit/{concept['type']}")
        related_links = ["[[fi-ml-moc]]", "[[fi-moc]]"]
    tags.extend(concept.get("tags", []))

    # Build content
    title = concept.get("title", slug.replace("-", " ").title())
    summary = concept.get("summary", "")
    details = concept.get("details", "")
    relevance = concept.get("relevance")
    owner = concept.get("owner")

    # Source link with folder prefix for proper resolution
    source_slug = slugify(source_citation)
    source_link = f"[[{folder}/{prefix}-{source_slug}]]"

    content = f"""---
tags:
  - {f"{chr(10)}  - ".join(tags)}
source: "{source_link}"
added: {date}
{_author_line()}---

# {title}

{summary}
"""

    if details:
        content += f"""
## Details

{details}
"""

    if relevance:
        content += f"""
## Fi Relevance

{relevance}
"""

    if owner:
        content += f"""
## Owner

{owner}
"""

    content += f"""
## Related

{chr(10).join(f"- {link}" for link in related_links)}

---

*Source: {source_citation}*
"""

    note_path.write_text(content)
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

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

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
        )
    print(f"  {len(extraction.get('notes', []))} concepts found")

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

    source_citation = extraction.get("key_citation", title)

    # Create source note
    source_note = create_source_note(extraction, file_path, output_dir, date, mode=mode)
    print(f"  Created: {source_note.name}")

    # Create concept notes
    concept_notes = []
    merged_notes = []
    for concept in extraction.get("notes", []):
        note_path = create_concept_note(concept, source_citation, output_dir, date, mode=mode, model=model)
        if note_path:
            # Check if this was a merge (note already existed) vs new creation
            if note_path.stem != f"{prefix}-{concept.get('slug', '')}":
                merged_notes.append(note_path)
                print(f"  Merged into: {note_path.name}")
            else:
                concept_notes.append(note_path)
                print(f"  Created: {note_path.name}")
        else:
            slug = concept.get("slug", "?")
            print(f"  Skipped: {prefix}-{slug}.md (nothing new to add)")

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
