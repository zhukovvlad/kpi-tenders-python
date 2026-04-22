#!/usr/bin/env python3
"""Quick sanity-check for the convert module.

Usage:
    python scripts/convert_check.py path/to/file.docx
    python scripts/convert_check.py path/to/file.xlsx

Calls the parsers directly — no Redis, no MinIO, no Go required.
Saves a .md file next to the source by default (use --no-save to skip).
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Make sure the project root is on sys.path when running directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.parsers.docx_parser import count_h1_h2, docx_to_markdown  # noqa: E402
from app.parsers.xlsx_parser import xlsx_to_markdown_sections  # noqa: E402


def _local_handle(file_bytes: bytes, path: Path) -> tuple[str, dict]:
    """Replicate _handle() locally without uploading to MinIO.

    Returns (markdown_content, meta_dict) where meta_dict mirrors the
    real result_payload but with md_storage_path as a local placeholder.
    """
    ext = path.suffix.lower()

    if ext == ".docx":
        content = docx_to_markdown(file_bytes)
        meta = {
            "format": "markdown",
            "md_storage_path": "<local — not uploaded to MinIO>",
            "char_count": len(content),
            "section_count": count_h1_h2(content),
        }
        return content, meta

    if ext == ".xlsx":
        sections = xlsx_to_markdown_sections(file_bytes)
        parts = [f"## Лист: {name}\n\n{table}" for name, table in sections]
        content = "\n\n".join(parts)
        meta = {
            "format": "markdown",
            "md_storage_path": "<local — not uploaded to MinIO>",
            "char_count": len(content),
            "sheet_count": len(sections),
        }
        return content, meta

    raise ValueError(f"Unsupported extension: {ext!r} (supported: .docx, .xlsx)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the convert parsers on a local file.")
    parser.add_argument("file", help="Path to a .docx or .xlsx file")
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Write Markdown to this file (default: same dir as source, .md extension)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save the .md file, only print the summary and a preview",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    file_bytes = path.read_bytes()

    try:
        content, meta = _local_handle(file_bytes, path)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    print("=== result_payload (md_storage_path would be set after MinIO upload) ===")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    if not args.no_save:
        out_path = Path(args.out) if args.out else path.with_suffix(".md")
        out_path.write_text(content, encoding="utf-8")
        print(f"\n[OK] Markdown saved → {out_path}  ({len(content)} chars)")
    else:
        preview = content[:2000]
        print(f"\n=== Markdown preview (first 2000 chars) ===\n{preview}")
        if len(content) > 2000:
            print(f"\n... [{len(content) - 2000} chars truncated]")


if __name__ == "__main__":
    main()
