#!/usr/bin/env python3
"""Quick sanity-check for the resolve_keys LLM module.

Calls Gemini directly — no Redis, no MinIO, no Go required.
Requires GEMINI_API_KEY in .env or environment.

Usage:
    python scripts/resolve_keys_check.py
    python scripts/resolve_keys_check.py --existing existing_keys.json
    python scripts/resolve_keys_check.py -q "Срок выполнения работ?" -q "Сумма договора?"

Examples:
    # Default built-in questions, no existing keys:
    python scripts/resolve_keys_check.py

    # Custom questions via CLI:
    python scripts/resolve_keys_check.py \
        -q "Какова общая площадь объекта?" \
        -q "Каков размер авансового платежа?" \
        -q "Дата окончания работ?"

    # With existing keys from a JSON file:
    python scripts/resolve_keys_check.py --existing existing_keys.json

    # existing_keys.json format:
    # [{"key_name": "total_square", "source_query": "Площадь?", "data_type": "number"}]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Make sure the project root is on sys.path when running directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.gemini_client import GeminiAPIError, get_client  # noqa: E402
from app.llm.resolve_keys_llm import resolve_keys  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_DEFAULT_QUESTIONS = [
    "Какова общая площадь объекта строительства?",
    "Каков размер авансового платежа?",
    "Дата окончания выполнения работ?",
    "Кто является заказчиком по договору?",
    "Какова стоимость договора (итого)?",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test resolve_keys LLM module against Gemini API."
    )
    parser.add_argument(
        "-q",
        "--question",
        metavar="TEXT",
        dest="questions",
        action="append",
        help="Question to resolve (can be repeated). Default: built-in set of 5 questions.",
    )
    parser.add_argument(
        "--existing",
        metavar="PATH",
        help="Path to a JSON file with existing keys "
        "(list of {key_name, source_query, data_type}).",
    )
    args = parser.parse_args()

    raw_questions: list[str] = args.questions or _DEFAULT_QUESTIONS

    existing_keys: list[dict] = []
    if args.existing:
        existing_path = Path(args.existing)
        if not existing_path.exists():
            print(f"[ERROR] File not found: {existing_path}", file=sys.stderr)
            sys.exit(1)
        try:
            existing_keys = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[ERROR] Cannot read file {existing_path}: {exc}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as exc:
            print(f"[ERROR] Invalid JSON in {existing_path}: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(existing_keys, list):
            print("[ERROR] existing keys file must contain a JSON array", file=sys.stderr)
            sys.exit(1)
        if not all(isinstance(k, dict) for k in existing_keys):
            print("[ERROR] each element in existing keys must be a JSON object", file=sys.stderr)
            sys.exit(1)

    print("─" * 60)
    print(f"Questions ({len(raw_questions)}):")
    for i, q in enumerate(raw_questions, 1):
        print(f"  {i}. {q}")
    print(f"\nExisting keys: {len(existing_keys)}")
    for k in existing_keys:
        print(f"  • {k.get('key_name')} ({k.get('data_type')})")
    print("─" * 60)

    try:
        client = get_client()
    except GeminiAPIError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    print("Calling Gemini...\n")

    try:
        result = resolve_keys(client, raw_questions=raw_questions, existing_keys=existing_keys)
    except GeminiAPIError as exc:
        print(f"[ERROR] Gemini API error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"[ERROR] Validation error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    print("─" * 60)
    print(f"resolved_schema ({len(result['resolved_schema'])} keys):")
    for entry in result["resolved_schema"]:
        print(f"  {entry['key_name']:30s}  type={entry['data_type']}")

    print(f"\nnew_keys ({len(result['new_keys'])} new):")
    for entry in result["new_keys"]:
        print(f"  {entry['key_name']:30s}  type={entry['data_type']}")
        print(f"    source: {entry['source_query']}")

    print("─" * 60)
    print("\nFull result_payload JSON:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
