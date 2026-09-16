"""Deterministic checks on a model's structured output, before it becomes an asset.

WHY THIS MODULE EXISTS
----------------------
The prompts in this repo carry a set of output rules: don't invent a figure,
don't invent a numbered plan, don't write about the requester, make your counts
match your lists. Those rules were prose for several releases and they did not
hold. A prompt rule is a request to a sampler, not a constraint: it lowers the
frequency of a behaviour without removing it, so across eight agents something
slipped on every single run. The failure was also self-renaming — banning
"Week 1" produced "Phase 1", banning that produced "Priority 1", then "Step 1
of 7", then "First… Second… Third…". Five rounds of prompt edits, five synonyms.

What DID hold, across the same runs, were the two rules implemented in code:
citation URLs verified against the evidence the model was actually shown
(`assets.build_sources`), and the long-term-memory write filter
(`context._insight_from`). That is the whole argument for this module.

WHAT IT IS
----------
Pure functions over the model's parsed payload plus the upstream text the model
was given. No IO, no model calls, no framework imports — so every rule is
testable against real observed output without a live run, which is the other
thing prompt rules could never offer.

`check()` returns Violations. It never mutates the payload and never raises: the
caller (`app/common/structured.ask_json`) decides what to do, which is to re-ask
the model once with the concrete violations and then record whatever could not be
repaired on the asset itself (`AssetEnvelope.ruleViolations`). Rejecting output
outright would be worse than the defect — a missing report is less useful to a
reviewer than a report carrying a visible note about what it got wrong.

THE UPSTREAM TEST
-----------------
Most rules here are not "this string is banned". They ask whether something in
the output is GROUNDED IN THE INPUT: a figure, or an ordinal label, is fine if it
appears in the upstream assets/evidence and invented if it does not. That is why
the rule survives the next synonym, and why `Stage 1 … Stage 4` passes (AWS
documentation defines those stages) while `Phase 1 … Phase 3` does not.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Semantic near-duplication ("compliance and regulatory constraints" vs
"regulatory/security requirements" — one concern, two entries) is not lexically
detectable, so `duplicate-in-list` catches near-verbatim repeats only and the
semantic case remains prompt-only. Likewise whether a claim is really a
calculation rather than a sourced fact. Those are named honestly rather than
implemented badly: a check with false positives costs a wasted repair call and a
worse output, so every rule below is deliberately tighter than the prose rule it
replaces.

Three further gaps are accepted by design, each the price of removing an observed
false positive, and each pinned by a test in tests/test_real_run_corpus.py:
  * a bare number with no unit or currency ("approve expenses under 5000") is not
    a figure here — see _FIGURE_RE
  * a count of `sources` is never checked, because the list is provenance and
    holds more than the agent's research — see _COUNT_TARGETS
  * a count that narrows to a subset ("the five prior recommendations") is not
    checked against the list length — see _NARROWING
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Violation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One broken rule, with enough detail to re-ask the model usefully.

    `detail` is written FOR THE MODEL: it quotes the offending text, because
    "you invented a numbered sequence" produced a different invented sequence,
    and "you wrote 'Phase 1', 'Phase 2' and no upstream asset contains those
    labels" produced dependency-ordered prose.
    """

    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


# ---------------------------------------------------------------------------
# Walking the payload
# ---------------------------------------------------------------------------

# Keys whose values are identifiers, provenance or controlled vocabulary rather
# than prose a reader consumes. Scanning them for "figures" would flag the digits
# in every assetId and every source URL.
_ID_KEYS = frozenset({
    "assetId", "assetType", "status", "createdAt", "createdByAgent", "version",
    "sourceAssetIds", "sourceAssetId", "artifactId", "artifactIds", "sourceId",
    "sourceRef", "sourceName", "url", "sectionId", "sectionType",
    "tracedToAssetIds", "priority", "confidence", "classification", "sourceType",
    "description",
})

# Fields that carry a RECOMMENDED ACTION, as opposed to a stated gap. The
# `action-on-unavailable` rule applies only to these: a `risks` or `limitations`
# entry is SUPPOSED to say a thing is unavailable, so scanning it for that phrase
# would punish the honest behaviour the rule exists to encourage.
_ACTION_SECTION_TYPES = frozenset({"recommendations", "next-steps"})

# DISCLOSURE fields. Their job is to state a premise the work rests on, or a gap
# in the evidence. That is the opposite of a defect — it is the honest behaviour
# every other rule here is trying to produce.
#
# This distinction is the root cause of the worst false positives observed live.
# The rules about what an asset may be ABOUT exist to stop an INVENTED claim being
# used as a finding or a rationale: "the user's background (hands-on with AWS
# serverless) suggests Lambda is feasible", for a request whose entire text was
# five words. They were never meant to stop an agent from carrying forward an
# assumption the brief itself declared. But the first live run flagged four of
# them — "The user wants a working application", "The user is familiar with basic
# AI/ML concepts" — because the check walked every string in the payload without
# asking what the field was FOR. Half that run's repair calls were spent deleting
# content that should have stayed.
#
# So: the about-* rules read CONTENT only. Figures and invented numbering are
# still checked everywhere, because an ungrounded cost is ungrounded wherever it
# is written.
_DISCLOSURE_KEYS = frozenset({
    "assumptions", "limitations", "dataLimitations", "risks", "openQuestions",
    "constraints", "keyQuestions",
})

_URL_RE = re.compile(r"https?://\S+")


def _clean(text: str) -> str:
    """Prose with URLs removed — a URL's digits and path segments are not figures."""
    return _URL_RE.sub(" ", text or "")


def _walk(obj, path: str = "", *, skip=_ID_KEYS) -> Iterator[tuple[str, str]]:
    """Yield (path, prose) for every reader-facing string in the payload."""
    if isinstance(obj, str):
        if obj.strip():
            yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in skip:
                continue
            yield from _walk(v, f"{path}.{k}" if path else str(k), skip=skip)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]", skip=skip)


def _walk_content(payload: dict) -> Iterator[tuple[str, str]]:
    """Only what the asset ASSERTS — the summary, the claims, the recommendations,
    the report sections. Not the fields whose purpose is to declare a premise or
    name a gap. See _DISCLOSURE_KEYS for why this distinction exists.
    """
    return _walk(payload, skip=_ID_KEYS | _DISCLOSURE_KEYS)


def _prose(payload: dict) -> str:
    """All reader-facing prose in one blob, for the whole-output rules."""
    return _clean("\n".join(s for _, s in _walk(payload)))


def _items_of(payload: dict) -> Iterator[tuple[str, str]]:
    """Yield (label, text) for each discrete list entry, for the per-entry rules.

    An entry is one recommendation, one risk, one section — the unit a reader
    reads as a single statement, and therefore the unit a contradiction has to
    live inside to be a real contradiction.
    """
    for key, value in payload.items():
        if key in _ID_KEYS or not isinstance(value, list):
            continue
        for i, entry in enumerate(value):
            label = f"{key}[{i}]"
            if isinstance(entry, str):
                yield label, _clean(entry)
            elif isinstance(entry, dict):
                text = "\n".join(s for _, s in _walk(entry))
                yield label, _clean(text)


def _action_items(payload: dict) -> Iterator[tuple[str, str]]:
    """Only the entries that express something the reader is being told to do."""
    for i, entry in enumerate(payload.get("items") or []):
        if isinstance(entry, dict):
            yield f"items[{i}]", _clean("\n".join(s for _, s in _walk(entry)))
    for i, entry in enumerate(payload.get("sections") or []):
        if isinstance(entry, dict):
            stype = str(entry.get("sectionType") or "").strip().lower()
            if stype in _ACTION_SECTION_TYPES:
                yield f"sections[{i}]({stype})", _clean(str(entry.get("content") or ""))


def _norm(text: str) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed — for comparisons."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _quote(text: str, limit: int = 160) -> str:
    one_line = re.sub(r"\s+", " ", text).strip()
    return f'"{one_line[:limit]}…"' if len(one_line) > limit else f'"{one_line}"'


# ---------------------------------------------------------------------------
# invented-numbering
# ---------------------------------------------------------------------------

# Broad on purpose. The list does not need to be exhaustive to be safe, because
# what makes a label a violation is the UPSTREAM TEST below, not membership here:
# a series that the inputs really define passes even though its noun is listed.
# That is what stops this becoming another denylist chasing the next synonym.
_SERIES_NOUNS = (
    "week", "phase", "priority", "step", "stage", "tier", "workstream",
    "milestone", "sprint", "wave", "track", "iteration", "horizon", "tranche",
    "cohort", "round", "band", "pillar", "epoch", "gate", "increment",
)
_SERIES_RE = re.compile(
    r"\b(" + "|".join(_SERIES_NOUNS) + r")s?\s*#?\s*(\d{1,2})\b", re.IGNORECASE)

_ORDINAL_WORDS = ("first", "second", "third", "fourth", "fifth", "sixth",
                  "seventh", "eighth", "ninth", "tenth")
# Sequence markers, not adjectives: "First, do X" is a step in a series the agent
# invented; "the first stage documented by AWS" is a reference to the input. The
# trailing comma (or 'ly,') is what distinguishes them.
_ORDINAL_MARKER_RE = re.compile(
    r"(?:^|[.\n;:]\s*)(" + "|".join(_ORDINAL_WORDS) + r")(?:ly)?\s*,", re.IGNORECASE)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# "one-phase" is not a series, so the compound starts at two.
_COMPOUND_RE = re.compile(
    r"\b(" + "|".join(k for k in _NUMBER_WORDS if k != "one") + r")[-\s]("
    + "|".join(_SERIES_NOUNS) + r")(?:ed|s)?\b", re.IGNORECASE)


def _upstream_has(canon_upstream: str, *variants: str) -> bool:
    return any(_norm(v) and _norm(v) in canon_upstream for v in variants)


def _upstream_series_size(upstream: str, noun: str) -> int:
    """How many distinct `<noun> <n>` labels the inputs actually define.

    This is what makes the compound check honest. Asking whether the literal
    phrase "four stages" appears upstream is the wrong question: AWS
    documentation defines Stage 1, Stage 2, Stage 3 and Stage 4 without ever
    writing "four stages", and an agent that reports "evolves through four
    stages" is COUNTING its evidence, not inventing a plan. Flagging that was a
    false positive in the first live run. A compound is invented only when the
    inputs do not contain that many members of the series.
    """
    nums = {int(n) for m, n in _SERIES_RE.findall(upstream or "")
            if m.lower() == noun.lower()}
    return len(nums)


def _check_numbering(payload: dict, upstream: str) -> list[Violation]:
    """A numbered or ordinal series the output invented to structure itself.

    Three shapes, one rule. A labelled series ('Phase 2'), an ordinal-word
    sequence ('First, … Second, …'), and a compound ('a three-phase approach').
    Each is a violation only when the labels are absent from the upstream text —
    so quoting a source's own 'Stage 1 … Stage 4' is fine, and minting
    'Priority 1 … Priority 5' over upstream high/medium/low is not.

    Plain enumeration ('1. …  2. …') of a list the inputs already contain is NOT
    flagged. It adds no commitment a reader can mistake for an agreed plan, and
    flagging it would make the check noisy enough to be turned off.
    """
    prose = _prose(payload)
    canon = _norm(upstream)
    out: list[Violation] = []

    # 1. Labelled series: group by noun, a series needs at least two members.
    by_noun: dict[str, set[int]] = {}
    for noun, num in _SERIES_RE.findall(prose):
        by_noun.setdefault(noun.lower(), set()).add(int(num))
    for noun, nums in sorted(by_noun.items()):
        if len(nums) < 2:
            continue
        ungrounded = sorted(n for n in nums
                            if not _upstream_has(canon, f"{noun} {n}", f"{noun}s {n}"))
        if ungrounded:
            labels = ", ".join(f"{noun.title()} {n}" for n in ungrounded)
            out.append(Violation(
                "invented-numbering",
                f"the output introduces the series {labels}, and no upstream input "
                f"contains those labels. Do not create a numbered sequence of your "
                f"own — carry the labels the inputs use, or order the work by "
                f"dependency and say so in prose."))

    # 2. Ordinal-word sequence used as a running order.
    markers = {m.group(1).lower() for m in _ORDINAL_MARKER_RE.finditer(prose)}
    if len(markers) >= 3:
        shown = ", ".join(w.title() for w in _ORDINAL_WORDS if w in markers)
        out.append(Violation(
            "invented-numbering",
            f"the output sequences its own content with the ordinal markers "
            f"{shown}. That reads as an agreed running order. State what depends "
            f"on what instead of numbering the steps."))

    # 3. Compound count ('a three-phase approach', 'seven steps'). Grounded either
    #    by the literal phrase OR by the inputs defining that many series members.
    for word, noun in _COMPOUND_RE.findall(prose):
        phrase = f"{word} {noun}"
        stated = _NUMBER_WORDS.get(word.lower(), 0)
        if _upstream_has(canon, phrase, f"{word}-{noun}"):
            continue
        if _upstream_series_size(upstream, noun) >= stated:
            continue  # counting the inputs' own series, not inventing one
        out.append(Violation(
            "invented-numbering",
            f'the output describes a "{phrase}" structure that no upstream input '
            f"defines. Do not summarise your own output as a numbered approach."))
    return out


# ---------------------------------------------------------------------------
# unsupported-figure
# ---------------------------------------------------------------------------

# A QUANTITY MARKER is required: currency, a percentage, or a unit. This is the
# second lesson from the first live run, where the rule flagged "(2025–2026)" —
# a publication-year range describing how recent the sources were.
#
# The rule's own justification is that "a number in a deliverable is read as a
# commitment". A year, a version (v0.4), a model name (Llama 3.3, Mixtral 8x22)
# and an enumeration index are identifiers: no reader mistakes them for a budget
# or a deadline. What CAN be mistaken always carries a marker — a currency
# symbol, a percent sign, or a unit of time, money, people or volume. Requiring
# the marker removes an entire class of false positives at the cost of missing a
# bare "approve expenses under 5000", which is both rare and less harmful than a
# check nobody trusts.
_UNITS = (
    r"second|minute|hour|day|week|fortnight|month|quarter|year|sprint|cycle"
    r"|fte|headcount|person|people|engineer|developer|user|customer|seat"
    r"|request|call|token|record|row|document|item|transaction"
    r"|dollar|usd|eur|gbp|pound|euro|cent"
    r"|ms|kb|mb|gb|tb|rps|qps|tps"
)
_FIGURE_RE = re.compile(
    r"""  [$£€]\s?\d[\d,]*(?:\.\d+)?                     # $5,000
      | \d[\d,]*(?:\.\d+)?\s?%                          # 85%
      | \b\d[\d,]*(?:\.\d+)?\s*[-–—]\s*\d[\d,]*(?:\.\d+)?\s*
        (?:""" + _UNITS + r""")s?\b                     # 6-12 months
      | \b\d[\d,]*(?:\.\d+)?\s*(?:""" + _UNITS + r""")s?\b   # 1,200 users
    """,
    re.VERBOSE | re.IGNORECASE)


def _canon_figure(text: str) -> str:
    """Comparison form: no spaces, no thousands separators, dashes unified.

    The dash normalisation is not cosmetic. `_FIGURE_RE` accepts a hyphen, an en
    dash or an em dash in a range, so "2-4 weeks" and "2\u20134 weeks" are the same
    quantity — but before this, a range written with one dash could never match
    the same range written upstream with another, and EVERY en-dash figure was
    reported ungrounded. Both sides of the comparison run through here, so the
    output is grounded when the quantity matches regardless of which dash either
    side chose.
    """
    return re.sub(r"[\s,]", "", re.sub(r"[\u2010-\u2015\u2212]", "-", text.lower()))


def _check_figures(payload: dict, upstream: str) -> list[Violation]:
    """A quantity the output states that the inputs never supplied.

    Costs, thresholds, percentages, durations. The check is presence in the
    upstream text, not plausibility, because the failure mode is a figure that
    reads to an approver as a requirement somebody agreed to — an illustrative
    "e.g. approve expenses under $5,000" is indistinguishable from a real limit
    once it is in a report.
    """
    canon_upstream = _canon_figure(_clean(upstream))
    seen: set[str] = set()
    out: list[Violation] = []
    for _, text in _walk(payload):
        for match in _FIGURE_RE.finditer(_clean(text)):
            token = match.group(0).strip()
            canon = _canon_figure(token)
            if canon in seen or canon in canon_upstream:
                continue
            seen.add(canon)
            out.append(Violation(
                "unsupported-figure",
                f'the figure "{token}" does not appear in any upstream input. '
                f"Remove it, or say plainly that no such figure was supplied — a "
                f"number in a deliverable is read as a commitment."))
    return out


# ---------------------------------------------------------------------------
# about-the-request
# ---------------------------------------------------------------------------

_ABOUT_REQUEST_RE = re.compile(
    r"\b(?:prior|previous|earlier|past)\s+runs?\b"
    r"|\brun\s+[0-9a-f]{8,}\b"
    r"|\bthis\s+(?:is\s+the\s+)?(?:first|second|third|fourth|fifth|sixth|\d+(?:st|nd|rd|th))\s+run\b"
    r"|\bthe\s+(?:user|requester|client)(?:'s)?\s+(?:has|have|is|are|was|were|seeks|"
    r"wants|prefers|knows|lacks|background|experience|familiar)\b"
    r"|\bthe\s+(?:user|requester|client)'s\b",
    re.IGNORECASE)


def _check_about_request(payload: dict) -> list[Violation]:
    """The deliverable making itself, or its requester, part of its content.

    Observed live: an analysis whose claims included "six prior runs have all
    completed successfully", and a recommendation carrying that into its risks.
    Also a recommendation justified by "the user's background (hands-on with AWS
    serverless)" for a request whose entire text was five words. Prior-run and
    recalled material is context for the agent; it is not a finding and never a
    rationale. No upstream test here — this is about the SUBJECT of a sentence,
    which grounding cannot license.

    CONTENT ONLY. An `assumptions` entry saying "we assumed the requester wants a
    working application" is a declared premise, and `limitations` naming what a
    prior run did not hand over is a declared gap. Both are the honest behaviour;
    flagging them was this rule's own worst defect. See _DISCLOSURE_KEYS.
    """
    out: list[Violation] = []
    seen: set[str] = set()
    for path, text in _walk_content(payload):
        for sentence in _sentences(text):
            if not _ABOUT_REQUEST_RE.search(sentence):
                continue
            key = _norm(sentence)[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(Violation(
                "about-the-request",
                f"{path} is about the requester or about this system's own run "
                f"history rather than about the subject: {_quote(sentence)}. "
                f"Remove it. Write about the topic."))
    return out


# ---------------------------------------------------------------------------
# finding-about-the-brief
# ---------------------------------------------------------------------------

# Matching only "request brief" and "the brief" let "The request identifies six
# key open questions" through in the first live run — the same defect wearing a
# shorter name. The subject of the sentence is what matters, so match the brief
# however it is referred to, followed by a verb of assertion.
_BRIEF_REF_RE = re.compile(
    r"\b(?:the\s+)?(?:current\s+|approved\s+)?request\s+brief\b"
    r"|\bthe\s+brief(?:'s)?\b"
    r"|\brequest\s+scope\b"
    r"|\bopenQuestions\b|\bkeyQuestions\b"
    r"|\b(?:the|this)\s+request(?:'s)?\s+"
    r"(?:identifies|identified|states|specifies|lists|defines|asks|flags|notes|"
    r"assumes|excludes|includes|scope|objective|open\s+questions|key\s+questions)\b",
    re.IGNORECASE)


def _check_findings_about_brief(payload: dict) -> list[Violation]:
    """A finding or claim whose subject is the brief instead of the evidence.

    Every downstream agent already has the brief. Restating it back as a research
    finding spends one of the few slots a reviewer reads on something that
    carries no new information — observed filling two of five findings, and two
    of six in another agent the same run. `dataLimitations` is the right home for
    "the brief does not specify X", so this rule only looks at findings/claims.
    """
    out: list[Violation] = []
    for key in ("findings", "claims"):
        for i, entry in enumerate(payload.get(key) or []):
            if not isinstance(entry, dict):
                continue
            statement = str(entry.get("statement") or "")
            if _BRIEF_REF_RE.search(_clean(statement)):
                out.append(Violation(
                    "finding-about-the-brief",
                    f"{key}[{i}] restates the request brief rather than reporting "
                    f"evidence or your own analysis: {_quote(statement)}. Every "
                    f"downstream reader already has the brief. Drop it, or move a "
                    f"genuine gap into the limitations list."))
    return out


# ---------------------------------------------------------------------------
# count-mismatch
# ---------------------------------------------------------------------------

# Phrase noun -> the payload list(s) it claims to count.
_COUNT_TARGETS: dict[str, tuple[str, ...]] = {
    "open questions": ("openQuestions",),
    "key questions": ("keyQuestions",),
    "data limitations": ("dataLimitations",),
    "recommendations": ("items",),
    "findings": ("findings",),
    "claims": ("claims",),
    "sections": ("sections",),
    "risks": ("risks",),
    "assumptions": ("assumptions",),
    "constraints": ("constraints",),
    "limitations": ("limitations", "dataLimitations"),
}
# NO "sources" ENTRY. `sources` is provenance, not a countable claim about the
# subject, and the two never line up: the list carries one entry per artifact the
# agent was given — including the request brief and any recalled context — so an
# agent that correctly reports "four research sources" is counting the research
# tools it consulted while the list holds five heterogeneous entries. Observed
# live as a false positive; the mismatch is in the question, not the output.

# A NARROWING MODIFIER means the phrase counts a SUBSET, so the list length is
# not the number being claimed. "the five prior recommendations" refers to what an
# earlier version carried, not to the six items in this payload; "three remaining
# open questions" is a residue. Flagged live against a 6-item list. Only counts
# with no such modifier are checked, which keeps the rule on the case it was built
# for — a stated total that contradicts the list right beside it.
_NARROWING = (
    "prior", "previous", "earlier", "past", "original", "remaining", "outstanding",
    "other", "additional", "further", "new", "unresolved", "upstream", "research",
    "first", "last", "next", "subsequent", "final", "top", "of",
)
# Up to two adjectives between the count and the noun, so "six critical open
# questions" and "three key risks" both resolve to the list they claim to count.
# The middle is captured so `_check_counts` can see a narrowing modifier.
_COUNT_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"((?:[\w/-]+\s+){0,2}?)"
    r"(" + "|".join(sorted(_COUNT_TARGETS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)


# The modifier sits on EITHER SIDE of the number: "five prior recommendations" but
# also "the first four findings", "the remaining three risks". Only the two words
# immediately before the count are considered, and never across a clause boundary,
# so "In the next section, five recommendations follow" is still a total.
_BEFORE_RE = re.compile(r"([\w/-]+(?:\s+[\w/-]+)?)\s*$")


def _narrows(middle: str, before: str = "") -> bool:
    """Does the phrasing restrict the count to a subset of the list?"""
    words = _norm(middle).split()
    clause = re.split(r"[.,;:!?\n]", before)[-1]
    match = _BEFORE_RE.search(clause)
    if match:
        words += _norm(match.group(1)).split()
    return any(w in _NARROWING for w in words)


def _stated_count(raw: str) -> int | None:
    """The integer a count phrase states, whether written as digits or a word."""
    if raw.isdigit():
        return int(raw)
    return _NUMBER_WORDS.get(raw.lower())


def _check_counts(payload: dict, extra_counts: dict[str, int]) -> list[Violation]:
    """A stated count that does not match the list it counts.

    Observed: "seven open questions" followed by six of them. Only checked when
    the count is resolvable — against the payload's own lists, or against a list
    the caller passes in (a synthesis agent counting the brief's open questions
    has no copy of them in its own payload).

    A count that NARROWS to a subset is not checked; see _NARROWING.
    """
    prose = _prose(payload)
    out: list[Violation] = []
    seen: set[tuple[str, int]] = set()
    for m in _COUNT_RE.finditer(prose):
        raw_n, middle, noun = m.group(1), m.group(2), m.group(3)
        stated = _stated_count(raw_n)
        if stated is None or _narrows(middle, prose[max(0, m.start() - 60):m.start()]):
            continue
        noun_l = noun.lower()
        actual: int | None = None
        for key in _COUNT_TARGETS[noun_l]:
            if isinstance(payload.get(key), list):
                actual = len(payload[key])
                break
            if key in extra_counts:
                actual = extra_counts[key]
                break
        if actual is None or actual == stated or (noun_l, stated) in seen:
            continue
        seen.add((noun_l, stated))
        out.append(Violation(
            "count-mismatch",
            f'the output says "{raw_n} {middle}{noun}" but there are {actual}. State the '
            f"count that matches the items you actually list, or omit the count."))
    return out


# ---------------------------------------------------------------------------
# duplicate-in-list
# ---------------------------------------------------------------------------

_DUPLICATE_RATIO = 0.86


def _entry_text(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("statement", "title", "detail", "content", "summary"):
            if str(entry.get(key) or "").strip():
                return str(entry[key])
    return ""


def _check_duplicates(payload: dict) -> list[Violation]:
    """The same entry twice in one list, near-verbatim.

    Lexical only. Two entries that say the same thing in different words are a
    real defect and are not detectable this way; that case stays a prompt rule
    and is called out in the module docstring rather than half-checked here.
    """
    out: list[Violation] = []
    for key, value in payload.items():
        if key in _ID_KEYS or not isinstance(value, list) or len(value) < 2:
            continue
        texts = [(i, _norm(_entry_text(e))) for i, e in enumerate(value)]
        texts = [(i, t) for i, t in texts if len(t) > 20]
        for a in range(len(texts)):
            for b in range(a + 1, len(texts)):
                ratio = difflib.SequenceMatcher(None, texts[a][1], texts[b][1]).ratio()
                if ratio >= _DUPLICATE_RATIO:
                    out.append(Violation(
                        "duplicate-in-list",
                        f"{key}[{texts[a][0]}] and {key}[{texts[b][0]}] are the "
                        f"same entry twice: {_quote(texts[b][1], 120)}. Merge them."))
    return out


# ---------------------------------------------------------------------------
# action-on-unavailable
# ---------------------------------------------------------------------------

_GONE = r"(?:retrieved|obtained|established|determined|evaluated|accessed|verified|reused)"
_UNAVAILABLE_RE = re.compile(
    r"\bunavailable\b"
    # "are not available", "may not be retrievable", "is not included"
    r"|\bno(?:t|)\s+(?:be\s+|been\s+)?"
    r"(?:available|retrievable|accessible|included|provided|obtainable|supplied)\b"
    # "cannot be retrieved", "could not be determined", "couldn't be obtained"
    r"|\bcannot\s+be\s+" + _GONE + r"\b"
    r"|\b(?:could|can|may|might|will|would)\s*n(?:ot|'t)\s+be\s+" + _GONE + r"\b"
    r"|\bno\s+(?:evidence|artifacts?|outputs?|access|record)\b"
    r"|\b(?:is|are|was|were)\s+missing\b",
    re.IGNORECASE)
# Verbs that ask the reader to go and get something. Bare "access" is deliberately
# absent: it shows up inside the gap statement itself ("without access to their
# outputs"), so including it would fire on the correct wording as well as the wrong.
_FETCH_RE = re.compile(
    r"\b(?:attempt to retrieve|retrieve|retrieving|consult|consulting|obtain|"
    r"obtaining|examine|examining|reuse|reusing|refer to|referring to|review|"
    r"reviewing|gather|gathering|collect|collecting)\b",
    re.IGNORECASE)


def _check_action_on_unavailable(payload: dict) -> list[Violation]:
    """An action item that tells the reader to fetch something it says is missing.

    Observed in both forms: the blunt "Consult the artifacts from the four prior
    runs" alongside "those artifacts are not available", and — after the prompt
    banned that — the hedged "Attempt to retrieve prior run artifacts, which may
    not be retrievable". Naming the same gap as both a step and its own caveat is
    the defect either way: the reader is handed an action they cannot take.

    Scoped to action-bearing fields only. A `risks` or `limitations` entry saying
    something is unavailable is exactly right, and must not be penalised.
    """
    out: list[Violation] = []
    for label, text in _action_items(payload):
        if _UNAVAILABLE_RE.search(text) and _FETCH_RE.search(text):
            out.append(Violation(
                "action-on-unavailable",
                f"{label} recommends obtaining something the same entry says is "
                f"unavailable: {_quote(text)}. An action the reader cannot take is "
                f"not a step. State what is missing and what having it would "
                f"unblock, in the limitations, and drop the action."))
    return out


# ---------------------------------------------------------------------------
# Profiles + entry point
# ---------------------------------------------------------------------------

INVENTED_NUMBERING = "invented-numbering"
UNSUPPORTED_FIGURE = "unsupported-figure"
ABOUT_REQUEST = "about-the-request"
FINDING_ABOUT_BRIEF = "finding-about-the-brief"
COUNT_MISMATCH = "count-mismatch"
DUPLICATE_IN_LIST = "duplicate-in-list"
ACTION_ON_UNAVAILABLE = "action-on-unavailable"

ALL_RULES = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, ABOUT_REQUEST, FINDING_ABOUT_BRIEF,
    COUNT_MISMATCH, DUPLICATE_IN_LIST, ACTION_ON_UNAVAILABLE,
})

# A research agent's subject may legitimately BE the run history (that is what
# the history_research agent is for), so ABOUT_REQUEST is off here. Its own
# observed defect — restating the brief as a finding — is covered.
RESEARCH = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, FINDING_ABOUT_BRIEF,
    COUNT_MISMATCH, DUPLICATE_IN_LIST,
})

# The three synthesis agents produce the reader-facing deliverable, so every rule
# applies, including the two about what a deliverable may be ABOUT.
SYNTHESIS = ALL_RULES

# The first agent has no upstream assets — its "upstream" is the raw request — so
# the rules that need a grounding baseline still work, but there is no brief to
# restate and nothing yet to recommend.
BRIEF = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, COUNT_MISMATCH, DUPLICATE_IN_LIST,
})


def check(payload: dict, *, upstream: str = "", rules=SYNTHESIS,
          extra_counts: dict[str, int] | None = None) -> list[Violation]:
    """Every violation in `payload`, given the `upstream` text the model was shown.

    `rules` selects which checks apply (see the profiles above). Never raises and
    never mutates: an unparseable or empty payload simply has no violations,
    because that case is already handled by the runners' degrade path.
    """
    if not isinstance(payload, dict) or not payload:
        return []
    extra_counts = extra_counts or {}
    found: list[Violation] = []
    if INVENTED_NUMBERING in rules:
        found += _check_numbering(payload, upstream)
    if UNSUPPORTED_FIGURE in rules:
        found += _check_figures(payload, upstream)
    if ABOUT_REQUEST in rules:
        found += _check_about_request(payload)
    if FINDING_ABOUT_BRIEF in rules:
        found += _check_findings_about_brief(payload)
    if COUNT_MISMATCH in rules:
        found += _check_counts(payload, extra_counts)
    if DUPLICATE_IN_LIST in rules:
        found += _check_duplicates(payload)
    if ACTION_ON_UNAVAILABLE in rules:
        found += _check_action_on_unavailable(payload)
    return found


# Per-rule cap on what goes into the re-ask. One ungrounded figure gets fixed; a
# wall of forty makes the instruction longer than the task and the model starts
# rewriting from scratch. The FULL list is still recorded on the asset — this caps
# only what we argue about.
_REPAIR_EXAMPLES_PER_RULE = 5


def as_repair_instructions(violations: list[Violation]) -> str:
    """The violations as a block to append to the user message for one re-ask.

    Concrete and quoted, because a generic restatement of the rule is what the
    model already had in its system prompt and ignored.
    """
    shown: dict[str, list[Violation]] = {}
    for v in violations:
        shown.setdefault(v.rule, []).append(v)
    lines: list[str] = []
    for rule, group in shown.items():
        for v in group[:_REPAIR_EXAMPLES_PER_RULE]:
            lines.append(f"- {v.detail}")
        if len(group) > _REPAIR_EXAMPLES_PER_RULE:
            lines.append(f"- ({len(group) - _REPAIR_EXAMPLES_PER_RULE} further "
                         f"{rule} problems of the same kind — fix them all.)")
    body = "\n".join(lines)
    return (
        "\n=== YOUR PREVIOUS ANSWER BROKE THESE RULES ===\n"
        f"{body}\n"
        "Return the COMPLETE corrected JSON — the same schema, the same substance, "
        "with only these problems fixed. Do not add a note about the correction, "
        "and do not drop content that was not at fault.\n"
    )
