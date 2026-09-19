#!/usr/bin/env python3
"""Generate app/workflow.schema.json from app/keys.json + app/vocabulary.json.

WHY A SCHEMA AT ALL, WHEN THREE PLANES ALREADY VALIDATE
Terraform, CDK and the container all reject a bad `workflow.json`, with good
messages. But all three reject it AFTER you have finished editing and run
something. For a customer meeting this framework for the first time, the questions
are earlier and simpler than that:

    which keys may this agent have?
    which of them are required?
    what values are allowed here?
    what does this one actually do?

A JSON Schema answers all four IN THE EDITOR, as you type, because `$schema` in
workflow.json points at it and every mainstream editor picks that up. Autocomplete
lists the legal keys for the `runtime` you chose, a wrong value is underlined with
the allowed set, and hovering a key shows what reads it. That is the difference
between a config file you can adopt and one you have to be taught.

WHY IT IS GENERATED AND NOT WRITTEN
A hand-written schema would be a FOURTH place encoding the same rules, and this
repo has spent a lot of effort deleting the second and third (see
app/vocabulary.json, which exists because the closed value sets were written out in
Python, TypeScript and HCL and drifted). A schema that disagreed with the
validators would be worse than none: an editor saying a config is fine and a deploy
rejecting it teaches a customer not to trust the editor.

So it is derived, `--check` keeps it in sync in CI, and a test asserts it accepts
the shipped workflow and rejects what the other planes reject.

    python3 build_schema.py            # regenerate
    python3 build_schema.py --check     # exit 1 if it is out of date (for CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent / "app"
KEYS = APP / "keys.json"
VOCAB = APP / "vocabulary.json"
OUT = APP / "workflow.schema.json"

#: JSON Schema draft. 2020-12 is what editors ship support for, and it is the draft
#: whose `if`/`then` and `dependentSchemas` express the per-variant key sets below.
DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _load() -> tuple[dict, dict]:
    return json.loads(KEYS.read_text()), json.loads(VOCAB.read_text())


def _property(spec: dict, vocab: dict) -> dict:
    """One key's schema: its type, its allowed values, and the text an editor shows.

    `description` carries BOTH halves — what the key does and what reads it — because
    the hover tooltip is the one place a customer reliably looks, and "which code
    consumes this" is exactly the question that otherwise sends them into the source.
    """
    out: dict = {"description": spec["doc"] + f"\n\nRead by: {spec['reads']}"}
    kind = spec.get("type")
    if kind:
        out["type"] = kind
    if kind == "array":
        out["items"] = {"type": spec.get("items", "string")}
        if spec.get("itemRequired"):
            out["items"]["required"] = spec["itemRequired"]

    def note(vocabulary: str, what: str) -> None:
        out["description"] += (
            f"\n\nAllowed {what} ({vocabulary} in app/vocabulary.json): "
            + ", ".join(repr(v) for v in vocab[vocabulary]["values"]))

    # THREE DIFFERENT THINGS A CLOSED SET CAN CONSTRAIN, and conflating them is how the
    # first version of this generator produced a schema that rejected the shipped file:
    # `guardrail.contentFilters` is a MAP whose values are strengths, and putting the
    # strength enum on the key itself asserted that the map WAS a strength.
    if spec.get("vocabulary"):
        target = out["items"] if kind == "array" else out
        target["enum"] = vocab[spec["vocabulary"]]["values"]
        note(spec["vocabulary"], "values")
    elif spec.get("enum"):
        out["enum"] = spec["enum"]
    if spec.get("valueVocabulary"):
        out["additionalProperties"] = {"enum": vocab[spec["valueVocabulary"]]["values"]}
        note(spec["valueVocabulary"], "values")
    if spec.get("keyVocabulary"):
        out["propertyNames"] = {"enum": vocab[spec["keyVocabulary"]]["values"]}
        note(spec["keyVocabulary"], "keys")

    if spec.get("pattern"):
        target = out["items"] if kind == "array" else out
        target["pattern"] = spec["pattern"]

    # An inline object with declared sub-keys, e.g. `domains: {include, exclude}`. Closed
    # like everything else, so a typo inside it is caught rather than ignored — which is
    # the whole reason collapsing four flat keys into one nested key is safe.
    if spec.get("properties"):
        out["additionalProperties"] = False
        out["properties"] = {
            name: ({"type": "array", "items": {"type": sub.get("items", "string")},
                    "description": sub["doc"]}
                   if sub.get("type") == "array"
                   else {"type": sub.get("type", "string"), "description": sub["doc"]})
            for name, sub in spec["properties"].items()
        }
    return out


def _nest(flat: dict[str, dict]) -> dict:
    """Turn dotted keys (`memory.longTerm`, `policy.tool`) into nested object schemas."""
    root: dict = {}
    for dotted, schema in flat.items():
        head, _, tail = dotted.partition(".")
        if not tail:
            root[head] = schema
            continue
        parent = root.setdefault(head, {"type": "object", "additionalProperties": False,
                                        "properties": {}})
        parent["properties"][tail] = schema
    return root


def _entry_schema(block: dict, vocab: dict) -> dict:
    """The schema for ONE agent / tool / step entry.

    The shape is: properties = every key the spec knows, so an editor can autocomplete
    all of them; then `allOf` of `if`/`then` clauses that NARROW by variant — the keys
    that do not apply to the chosen `runtime` or `type` are forbidden there rather than
    merely undocumented. That ordering matters for usability: a customer who has not
    yet typed `runtime` still gets completions, and one who has gets only the right
    ones.
    """
    keys = block["keys"]
    variant_key = block.get("$variantKey")
    props = _nest({k: _property(v, vocab) for k, v in keys.items()})

    schema: dict = {"type": "object", "additionalProperties": False, "properties": props}

    universal_required = [k for k, v in keys.items() if v.get("required")]
    if universal_required:
        schema["required"] = universal_required

    rules: list[dict] = []

    # Exactly-one groups (agentCard vs source; lambdaArn vs source; agent vs parallel
    # vs sequence). `oneOf` over single-key `required` is how JSON Schema says XOR.
    for rule in block.get("$exactlyOne") or []:
        clause = {
            "oneOf": [{"required": [k]} for k in rule["keys"]],
            "$comment": rule["why"],
        }
        applies = rule.get("appliesTo")
        if applies == "*":
            rules.append(clause)
        else:
            rules.append({"if": {"properties": {variant_key: {"enum": applies}},
                                 "required": [variant_key]},
                          "then": clause})

    if variant_key:
        variants = _variants(block, vocab)
        aliases = block.get("$variantAliases") or {}
        for variant in variants:
            allowed, required = [], []
            for key, spec in keys.items():
                scope = spec.get("appliesTo")
                if scope == "*" or variant in _expand(scope, aliases):
                    allowed.append(key)
                if variant in _expand(spec.get("requiredFor") or [], aliases):
                    required.append(key)
            head = sorted({k.partition(".")[0] for k in allowed})
            clause: dict = {"properties": {k: props[k] for k in head},
                            "additionalProperties": False}
            if required:
                clause["required"] = sorted({k.partition(".")[0] for k in required})
            rules.append({
                "if": {"properties": {variant_key: {"const": variant}},
                       **({} if variant == block.get("$variantDefault")
                          else {"required": [variant_key]})},
                "then": clause,
            })

    if rules:
        schema["allOf"] = rules
    return schema


def _expand(scope, aliases: dict) -> list[str]:
    """`appliesTo` with any alias (`local` -> main, dedicated) resolved."""
    if scope == "*" or scope is None:
        return []
    out: list[str] = []
    for name in scope:
        out += aliases.get(name, [name])
    return out


def _variants(block: dict, vocab: dict) -> list[str]:
    """The variant values this block branches on, from the vocabulary that closes them."""
    key = block["$variantKey"]
    spec = block["keys"][key]
    if spec.get("vocabulary"):
        return list(vocab[spec["vocabulary"]]["values"])
    return list(spec.get("enum") or [])


def build() -> dict:
    keys, vocab = _load()
    agent = _entry_schema(keys["agent"], vocab)
    # The feature block is its own spec, spliced in where the agent declares it, so
    # `agentcore` gets completions and per-key hover text too instead of being an
    # opaque object.
    agent["properties"]["agentcore"] = {
        **_entry_schema(keys["agentcore"], vocab),
        "description": keys["agent"]["keys"]["agentcore"]["doc"],
    }
    # `additionalProperties: false` inside each variant clause lists the keys allowed
    # there, and `agentcore` is in every one of them — so the spliced schema has to
    # replace the placeholder in those lists as well.
    for rule in agent.get("allOf") or []:
        then = rule.get("then") or {}
        if "agentcore" in (then.get("properties") or {}):
            then["properties"]["agentcore"] = agent["properties"]["agentcore"]

    return {
        "$schema": DRAFT,
        "$id": "https://github.com/awslabs/agentcore-samples/multi-agent-orchestrator/workflow.schema.json",
        "title": "Multi-Agent Orchestrator workflow",
        "description": (
            "GENERATED — do not edit. Run `python3 build_schema.py` after changing "
            "app/keys.json or app/vocabulary.json. This exists so your editor can tell "
            "you which keys are legal, which are required and what values they take, "
            "before you deploy rather than after."),
        "type": "object",
        "additionalProperties": False,
        "required": ["agents", "steps"],
        "properties": {
            "$schema": {"type": "string",
                        "description": "Points your editor at this file."},
            "$comment": {"type": "string",
                         "description": "Orientation for whoever opens the file. Not read "
                                        "by anything."},
            "agents": {
                "type": "object",
                "description": "One entry per agent, keyed by agent id.\n\n"
                               + keys["agent"]["$comment"],
                "propertyNames": {"pattern": "^[a-zA-Z][a-zA-Z0-9_]*$"},
                "additionalProperties": agent,
            },
            "tools": {
                "type": "object",
                "description": "One entry per data source.\n\n" + keys["tool"]["$comment"],
                "additionalProperties": _entry_schema(keys["tool"], vocab),
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "description": keys["step"]["$comment"],
                "items": _entry_schema(keys["step"], vocab),
            },
            **{name: {**_entry_schema(keys[name], vocab),
                      "description": keys[name]["$comment"]}
               for name in ("orchestrator", "ui", "guardrail", "authorization")},
        },
    }


def main() -> int:
    out = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current == out:
            print(f"build_schema: {OUT.name} is up to date")
            return 0
        print(f"build_schema: {OUT.name} is out of date (run: python3 "
              f"{Path(__file__).name})", file=sys.stderr)
        return 1
    OUT.write_text(out)
    print(f"build_schema: wrote {OUT.name} ({out.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
