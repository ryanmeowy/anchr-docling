import re as _re
from typing import Any

from anchr_docling.errors import add_warning
from anchr_docling.schemas import ParseOptions, ParsedPage, ParseWarning

def export_native_chunks(
    document: Any,
    options: ParseOptions,
    warnings: list[ParseWarning],
) -> list[dict[str, Any]]:
    """Chunk using Docling's native HybridChunker (bbox + token-aware)."""
    try:
        from docling_core.transforms.chunker import HybridChunker
        from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
    except ImportError:
        add_warning(
            warnings,
            code="native_chunker_unavailable",
            message="docling chunker is not installed.",
        )
        return []

    # Concrete tokenizer for HybridChunker.  Must extend BaseTokenizer (Pydantic model).
    class _EstimateTokenizer(BaseTokenizer):
        def count_tokens(self, text: str) -> int:
            return _estimate_tokens(text)

        def get_max_tokens(self) -> int:
            return getattr(self, "_max_tokens", 800)

        def get_tokenizer(self) -> Any:
            # semchunk expects Callable[[str], int]
            return _estimate_tokens

    tokenizer = _EstimateTokenizer()
    object.__setattr__(tokenizer, "_max_tokens", options.chunk_max_tokens)

    hy_chunker = HybridChunker(
        tokenizer=tokenizer,
        merge_peers=True,
    )

    chunks: list[dict[str, Any]] = []

    for i, chunk in enumerate(hy_chunker.chunk(document)):
        text = chunk.text.strip()
        if not text:
            continue

        md_text = text
        plain_text = strip_markdown(text)

        # Collect bboxes and page numbers from doc_items.
        doc_items: list[Any] = getattr(chunk.meta, "doc_items", []) or []
        bboxes: list[dict[str, Any]] = []
        page_nums: set[int] = set()

        for item in doc_items:
            prov_list = getattr(item, "prov", []) or []
            for prov in prov_list:
                page_no = getattr(prov, "page_no", None)
                bbox_obj = getattr(prov, "bbox", None)
                if page_no is None and bbox_obj is None:
                    continue
                bbox_entry: dict[str, Any] = {}
                if page_no is not None:
                    bbox_entry["pageNo"] = page_no
                    page_nums.add(page_no)
                if bbox_obj is not None:
                    bbox = bbox_obj.model_dump(mode="json") if hasattr(bbox_obj, "model_dump") else bbox_obj
                    bbox_entry["bbox"] = bbox
                if bbox_entry:
                    bboxes.append(bbox_entry)

        headings: list[str] = getattr(chunk.meta, "headings", None) or []

        chunk_data: dict[str, Any] = {
            "chunkId": f"chunks/{len(chunks)}",
            "type": "section",
            "text": md_text,
            "textPlain": plain_text,
            "charCount": len(md_text),
            "source": "native",
            "bboxes": bboxes,
        }

        if page_nums:
            chunk_data["pageRange"] = [min(page_nums), max(page_nums)]
        if headings:
            chunk_data["headings"] = headings

        chunks.append(chunk_data)

    return chunks


def _estimate_tokens(text: str) -> int:
    """Rough token count for Chinese/English mixed text.

    1 token ≈ 1.5 chars (conservative estimate for CJK + English).
    """
    return max(1, int(len(text) / 1.5))


def export_markdown_chunks(
    full_text: str,
    pages: list[ParsedPage],
    options: ParseOptions,
    warnings: list[ParseWarning],
) -> list[dict[str, Any]]:
    """Chunk markdown text with structure-aware splitting.

    Headings (##/###) act as strong boundaries.  Tables and images are kept
    intact.  Each chunk carries both the raw markdown (``text``, for LLM
    consumption) and a plain-text variant (``textPlain``, for embedding).
    """
    target_chars = max(1, options.chunk_min_tokens * 2)
    max_chars = max(target_chars, options.chunk_max_tokens * 2)

    canonical = full_text.strip()
    if not canonical:
        return []

    blocks = parse_markdown_blocks(canonical)
    if not blocks:
        return []

    spans = _build_markdown_page_spans(canonical, pages)

    chunks: list[dict[str, Any]] = []
    current_blocks: list[dict[str, Any]] = []

    def _cur_len() -> int:
        return len(join_blocks_text(current_blocks))

    for block in blocks:
        cur_len = _cur_len()

        # Table that exceeds max_chars on its own: emit as single chunk.
        if block["type"] == "table" and len(block["text"]) > max_chars:
            if current_blocks:
                chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
                current_blocks = []
            chunks.append(_build_md_chunk(len(chunks), [block], spans))
            continue

        # Heading forces flush when we already have enough content.
        if block["type"] == "heading" and cur_len >= target_chars:
            chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
            current_blocks = []

        # Would overflow: flush current batch.
        if current_blocks and cur_len + len(block["text"]) > max_chars and cur_len >= target_chars:
            chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
            current_blocks = []

        current_blocks.append(block)

    if current_blocks:
        chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))

    for chunk in chunks:
        chunk["source"] = "native"
    return chunks


def parse_markdown_blocks(md_text: str) -> list[dict[str, Any]]:
    """Parse markdown text into typed logical blocks.

    Returns blocks like:
      {"type": "heading", "text": "## Overview", "level": 2, "offset": 42}
      {"type": "paragraph", "text": "Some text.", "offset": 78}
      …
    ``offset`` is the byte position of the block's text within *md_text*.
    """
    parsed: list[dict[str, Any]] = []
    pos = 0
    n = len(md_text)

    while pos < n:
        # Skip leading whitespace between blocks.
        while pos < n and md_text[pos] in ("\n", " ", "\t", "\r"):
            pos += 1
        if pos >= n:
            break

        # Find the next \n\n boundary.
        end = md_text.find("\n\n", pos)
        if end == -1:
            end = n

        block_text = md_text[pos:end].strip()
        if not block_text:
            pos = end + 2 if end < n else n
            continue

        line = block_text.split("\n", 1)[0]

        if line.startswith("```"):
            parsed.append({"type": "code", "text": block_text, "offset": pos})
        elif line.startswith("<!-- image -->") or line.startswith("!["):
            parsed.append({"type": "image", "text": block_text, "offset": pos})
        elif line.startswith("|"):
            parsed.append({"type": "table", "text": block_text, "offset": pos})
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            parsed.append({"type": "heading", "text": block_text, "level": level, "offset": pos})
        elif line in ("---", "***", "___") or set(line) <= {"-", "*", "_", " "}:
            pass  # horizontal rule – skip
        elif line.startswith(("- ", "* ")) or (line[0].isdigit() and ". " in line[:4]):
            parsed.append({"type": "list_item", "text": block_text, "offset": pos})
        else:
            parsed.append({"type": "paragraph", "text": block_text, "offset": pos})

        pos = end + 2 if end < n else n

    return parsed


def join_blocks_text(blocks: list[dict[str, Any]]) -> str:
    return "\n\n".join(b["text"] for b in blocks if b.get("text"))


def _build_md_chunk(
    index: int,
    blocks: list[dict[str, Any]],
    page_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    md_text = join_blocks_text(blocks)
    plain = strip_markdown(md_text)

    chunk: dict[str, Any] = {
        "chunkId": f"chunks/{index}",
        "type": "section",
        "text": md_text,
        "textPlain": plain,
        "charCount": len(md_text),
    }

    page_range = _resolve_chunk_page_range(blocks, page_spans)
    if page_range is not None:
        chunk["pageRange"] = page_range

    return chunk


def _build_markdown_page_spans(
    full_text: str,
    pages: list[ParsedPage],
) -> list[dict[str, Any]]:
    """Map page markdown texts to character offsets in the full markdown."""
    spans: list[dict[str, Any]] = []
    cursor = 0
    for page in pages:
        page_text = page.text.strip()
        if not page_text:
            continue
        found = full_text.find(page_text, cursor)
        if found >= 0:
            start = found
            end = found + len(page_text)
        else:
            start = min(cursor, len(full_text))
            end = min(len(full_text), start + len(page_text))
        spans.append(
            {"start": start, "end": end, "pageNo": page.page_no}
        )
        cursor = end
    return spans


def _resolve_page_no(
    offset: int,
    page_spans: list[dict[str, Any]],
) -> int | None:
    """Return the page number for a character offset."""
    for span in page_spans:
        if span["start"] <= offset < span["end"]:
            return span.get("pageNo")
    if page_spans:
        nearest = min(page_spans, key=lambda s: abs(s["start"] - offset))
        return nearest.get("pageNo")
    return None


def _resolve_chunk_page_range(
    blocks: list[dict[str, Any]],
    page_spans: list[dict[str, Any]],
) -> list[int] | None:
    """Determine page range for a chunk from its blocks' offsets."""
    page_nums: set[int] = set()
    for block in blocks:
        offset: int = block.get("offset", 0)
        pn = _resolve_page_no(offset, page_spans)
        if isinstance(pn, int):
            page_nums.add(pn)
    if not page_nums:
        return None
    return [min(page_nums), max(page_nums)]


def strip_markdown(text: str) -> str:
    """Strip markdown formatting to produce clean text for embedding."""
    import re as _re

    t = text
    # Remove code fences with content
    t = _re.sub(r"```[^`]*```", "", t)
    # Remove images: ![alt](url) and <!-- image -->
    t = _re.sub(r"!\[.*?\]\(.*?\)", "", t)
    t = t.replace("<!-- image -->", "")
    # Remove links, keep text: [text](url)
    t = _re.sub(r"\[([^\]]*)\]\(.*?\)", r"\1", t)
    # Strip heading markers
    t = _re.sub(r"^#{1,6}\s+", "", t, flags=_re.MULTILINE)
    # Bold / italic
    t = _re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = _re.sub(r"__(.+?)__", r"\1", t)
    t = _re.sub(r"\*(.+?)\*", r"\1", t)
    t = _re.sub(r"_(.+?)_", r"\1", t)
    # Inline code
    t = _re.sub(r"`([^`]+)`", r"\1", t)
    # Table formatting: strip pipes and alignment row
    t = _re.sub(r"\|", " ", t)
    t = _re.sub(r"\s{2,}", " ", t)
    # Clean up blank lines
    t = _re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

