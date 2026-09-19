"""The framework's closed value sets, read from app/vocabulary.json.

WHY THIS IS A FILE AND NOT A PYTHON CONSTANT. Every one of these lists used to be
written out two or three times: once here in Python, once in `cdk/lib/*.ts`, and once
in `terraform/*.tf`. `toolTypes` and the RBAC actions existed in all three.

That is a real cost and it caused real bugs. Adding a value meant finding every copy,
and a copy that got missed rejected a config the other planes accepted — so whether a
workflow deployed depended on which IaC path you used. A parity test existed purely to
compare the copies against each other, which is a test that a duplicate has not drifted
rather than a reason the duplicate exists.

JSON is the one format all three planes read natively: `json.load` here,
`require()`/`JSON.parse` in TypeScript, `jsondecode(file(...))` in HCL. So the list is
held once and every plane reads it.

This module is separate from `config.py` on purpose. `config.py` holds the CUSTOMER'S
workflow; this holds the FRAMEWORK's vocabulary. Keeping them apart is what stops a
customer's file from being able to widen a set the framework enforces.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Sits beside app/workflow.json so both IaC paths can reach it the same way they
#: already reach the workflow, and so `COPY app ./app` puts it in the image.
_PATH = Path(__file__).resolve().parent.parent / "vocabulary.json"


def _load() -> dict[str, tuple[str, ...]]:
    raw = json.loads(_PATH.read_text())
    return {
        name: tuple(block.get("values") or ())
        for name, block in raw.items()
        if not name.startswith("$")
    }


_VOCAB = _load()


def values(name: str) -> tuple[str, ...]:
    """The closed set called `name`. Raises if it is not declared, rather than
    returning an empty tuple — an empty set would make every value invalid, or every
    value valid, depending on how the caller uses it. Neither is a good silent
    default."""
    try:
        return _VOCAB[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a vocabulary in app/vocabulary.json. Declared: "
            f"{', '.join(sorted(_VOCAB))}") from None


# Named bindings, so a reader sees the vocabulary at the point of use rather than a
# string lookup, and so a typo is an ImportError instead of a KeyError at runtime.
RUNTIMES = values("runtimes")
TOOL_TYPES = values("toolTypes")
TOOL_SCHEMA_PROPERTY_TYPES = values("toolSchemaPropertyTypes")
TOOL_AUTH_MODES = values("toolAuthModes")
API_KEY_TOOL_TYPES = values("apiKeyToolTypes")
TOOL_LISTING_MODES = values("toolListingModes")
A2A_AUTH_MODES = values("a2aAuthModes")
A2A_SOURCES = values("a2aSources")
A2A_LAMBDA_SKILLS = values("a2aLambdaSkills")
MEMORY_STRATEGIES = values("memoryStrategies")
AUTHORIZATION_ACTIONS = values("authorizationActions")
WEB_SEARCH_REGIONS = values("webSearchRegions")
GUARDRAIL_FILTER_STRENGTHS = values("guardrailFilterStrengths")
GUARDRAIL_PII_ACTIONS = values("guardrailPiiActions")
BUILTIN_LAMBDA_SOURCE = values("builtinLambdaSource")[0]
