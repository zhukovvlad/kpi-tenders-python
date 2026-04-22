"""DOCX → Markdown converter.

Iterates body blocks in document order so that paragraphs and tables
appear in the output in the same sequence as in the original file.
"""

import re
from io import BytesIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_HEADING_MAP: dict[str, str] = {
    "Heading 1": "#",
    "Heading 2": "##",
    "Heading 3": "###",
}


def docx_to_markdown(file_bytes: bytes) -> str:
    """Convert DOCX bytes to a Markdown string, preserving document order."""
    doc = Document(BytesIO(file_bytes))
    blocks: list[str] = []

    for child in doc.element.body:
        tag = child.tag
        if tag == qn("w:p"):
            # Pass `doc` (not body) so Paragraph.part resolves to DocumentPart.
            para = Paragraph(child, doc)
            md = _paragraph_to_md(para)
            if md:
                blocks.append(md)
        elif tag == qn("w:tbl"):
            table = Table(child, doc)
            md = _table_to_md(table)
            if md:
                blocks.append(md)

    return "\n\n".join(blocks)


def count_h1_h2(markdown: str) -> int:
    """Count H1 and H2 headings (lines starting with `# ` or `## `)."""
    return len(re.findall(r"^#{1,2} ", markdown, re.MULTILINE))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _runs_to_md(runs) -> str:  # noqa: ANN001
    """Convert a paragraph's runs to inline Markdown."""
    parts: list[str] = []
    for run in runs:
        text = run.text
        if not text:
            continue
        if run.bold and run.italic:
            # Strip inner whitespace: CommonMark requires delimiter runs not to
            # have leading/trailing spaces (" **hello** " is not rendered as bold).
            text = f"***{text.strip()}***"
        elif run.bold:
            text = f"**{text.strip()}**"
        elif run.italic:
            text = f"*{text.strip()}*"
        parts.append(text)
    return "".join(parts)


def _paragraph_to_md(para: Paragraph) -> str:
    style_name = para.style.name if para.style else ""
    text = _runs_to_md(para.runs)

    if not text.strip():
        return ""

    heading_prefix = _HEADING_MAP.get(style_name)
    if heading_prefix:
        return f"{heading_prefix} {text.strip()}"

    if style_name.startswith("List Bullet"):
        return f"- {text.strip()}"

    if style_name.startswith("List Number"):
        return f"1. {text.strip()}"

    return text


def _table_to_md(table: Table) -> str:
    if not table.rows:
        return ""

    table_rows: list[list[str]] = []
    for row in table.rows:
        # python-docx repeats the same _Cell object for each column a merged
        # cell spans.  Deduplicate by the underlying XML element identity.
        # Note: cell._tc is a private lxml attribute; it's stable across
        # current python-docx versions but may change in future releases.
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            cid = id(cell._tc)
            if cid in seen:
                continue
            seen.add(cid)
            cells.append(cell.text.strip().replace("\n", " ").replace("|", "\\|"))
        table_rows.append(cells)

    # Use max visible cells across all rows so the separator always covers
    # the widest row (important when a merged header has fewer cells than data rows).
    col_count = max(len(cells) for cells in table_rows)
    rows_md: list[str] = []
    for i, cells in enumerate(table_rows):
        padded = (cells + [""] * col_count)[:col_count]
        rows_md.append("| " + " | ".join(padded) + " |")
        if i == 0:
            rows_md.append("| " + " | ".join(["---"] * col_count) + " |")

    return "\n".join(rows_md)
