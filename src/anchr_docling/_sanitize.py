"""Markdown text sanitizer for formula-related OCR artifacts."""

import re

# Match entire lines that start with "ParseError: KaTeX parse error:"
_PARSE_ERROR_LINE = re.compile(r"^ParseError:\s*KaTeX\s+parse\s+error:.*$", re.MULTILINE)

# Match $$...$$ display math blocks.  Group 1 = inner content.
# Handles blocks that may span multiple lines.
_DISPLAY_MATH_BLOCK = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def sanitize_markdown_text(text: str) -> str:
    """Clean up OCR-extracted markdown text with formula-related artifacts.

    Handles three classes of noise commonly found when Docling OCR processes
    documents that contain mathematical formulas:

    1. ``<!-- formula-not-decoded -->`` placeholder comments.
    2. ``ParseError: KaTeX parse error: ...`` lines (from KaTeX errors
       embedded in the source PDF).
    3. Unescaped ``$`` characters inside ``$$...$$`` display math blocks that
       would cause downstream KaTeX rendering to fail.
    """

    # 1. Strip formula-not-decoded placeholders.
    text = text.replace("<!-- formula-not-decoded -->", "")

    # 2. Remove entire ParseError lines.
    text = _PARSE_ERROR_LINE.sub("", text)

    # 3. Escape stray $ inside $$...$$ blocks.
    text = _DISPLAY_MATH_BLOCK.sub(_escape_inner_dollars, text)

    # Collapse blank lines that may have been left behind.
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _escape_inner_dollars(match: re.Match[str]) -> str:
    """Escape unescaped ``$`` in the inner content of a ``$$...$$`` block."""
    inner = match.group(1)

    # Replace $ that is NOT preceded by a backslash with \$
    inner = re.sub(r"(?<!\\)\$", r"\\$", inner)

    return f"$${inner}$$"
