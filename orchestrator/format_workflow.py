#!/usr/bin/env python3
"""Format app/workflow.json for humans to read.

workflow.json is THE file a customer edits, so it is worth formatting for reading
rather than for a machine. `json.dump(indent=2)` gets two things wrong for this
file:

  * it escapes every non-ASCII character, turning the em-dashes and the emoji in
    the explanatory notes into \\u2014 and \\ud83e\\udded — unreadable in the one
    place the prose actually matters;
  * it explodes small leaf objects over five lines each, so a schema property
    definition or a set of content-filter strengths takes a screenful.

This rewrites it so that:
  * text is real UTF-8;
  * a short object/array with no nested containers goes on ONE line;
  * anything longer, or with nesting, stays expanded at 2-space indent.

Key order is preserved exactly — it is meaningful here (the `*Note` keys sit
beside what they explain).

    python3 format_workflow.py            # format in place
    python3 format_workflow.py --check    # exit 1 if it would change (for CI)

Run it after any programmatic edit to the file.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

PATH = Path(__file__).resolve().parent / "app" / "workflow.json"
INDENT = "  "
# A leaf collection is inlined only if it fits this width including its indent.
# 100 keeps the file comfortable in a side-by-side diff.
MAX_INLINE = 100


def _is_leaf(v) -> bool:
    """True when `v` contains no dict or list, so inlining it loses no structure."""
    if isinstance(v, dict):
        return all(not isinstance(x, (dict, list)) for x in v.values())
    if isinstance(v, list):
        return all(not isinstance(x, (dict, list)) for x in v)
    return True


def _scalar(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _inline(v) -> str:
    """One-line form, with a space inside the braces so it reads as a unit."""
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_scalar(k)}: {_scalar(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    return _scalar(v)


def render(v, depth: int = 0) -> str:
    pad = INDENT * depth
    inner = INDENT * (depth + 1)

    if isinstance(v, (dict, list)) and _is_leaf(v):
        one = _inline(v)
        # +2 for a trailing comma and a little slack.
        if len(pad) + len(one) + 2 <= MAX_INLINE:
            return one

    if isinstance(v, dict):
        if not v:
            return "{}"
        parts = [f"{inner}{_scalar(k)}: {render(x, depth + 1)}" for k, x in v.items()]
        return "{\n" + ",\n".join(parts) + f"\n{pad}}}"

    if isinstance(v, list):
        if not v:
            return "[]"
        parts = [f"{inner}{render(x, depth + 1)}" for x in v]
        return "[\n" + ",\n".join(parts) + f"\n{pad}]"

    return _scalar(v)


def main() -> int:
    doc = json.loads(PATH.read_text(), object_pairs_hook=OrderedDict)
    out = render(doc) + "\n"

    # Never let formatting change meaning.
    if json.loads(out) != doc:
        print("format_workflow: refusing to write - round-trip changed the document",
              file=sys.stderr)
        return 2

    if "--check" in sys.argv:
        if PATH.read_text() == out:
            print(f"format_workflow: {PATH.name} is formatted")
            return 0
        print(f"format_workflow: {PATH.name} needs formatting (run: python3 "
              f"{Path(__file__).name})", file=sys.stderr)
        return 1

    before = PATH.read_text()
    PATH.write_text(out)
    lines_before, lines_after = before.count("\n"), out.count("\n")
    print(f"format_workflow: {PATH.name} {lines_before} -> {lines_after} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
