"""Lossless bounded text sections with exact source revision/character anchors."""

import hashlib
import re


def sections(text, *, size=24000, max_sections=512):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Document text is empty; use a visual/OCR conversion explicitly")
    if size < 256:
        raise ValueError("Section size is too small")
    source_revision = hashlib.sha256(text.encode()).hexdigest()
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Retain page/table whitespace; choose a heading/paragraph boundary
            # when available and hard-split otherwise without dropping bytes.
            candidates = [m.start() + start for m in re.finditer(r"\n(?=#{1,6} )|\n\n|\f", text[start:end])]
            good = [position for position in candidates if position > start + size // 2]
            if good:
                end = good[-1]
        body = text[start:end]
        result.append({"start": start, "end": end, "text": body,
                       "revision": hashlib.sha256(body.encode()).hexdigest(),
                       "source_revision": source_revision,
                       "anchor": f"chars:{start}-{end}@{source_revision}"})
        if len(result) > max_sections:
            raise ValueError("Document exceeds bounded extraction; split into explicit source parts")
        start = end
    return result
