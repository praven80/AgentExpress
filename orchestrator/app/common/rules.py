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


# Abbreviations that end in a period without ending a sentence. Splitting on them
# truncated an enumeration mid-list: "four decisions remain deferred: ingestion
# service choice, processing strategy (Lambda vs. Glue), storage layout, query
# engine" was cut after "vs." and the remaining two items were reported as
# contradicting the count. Technical prose is full of these.
_ABBREV = ("vs", "e.g", "i.e", "etc", "cf", "approx", "no", "fig", "eq", "al",
           "inc", "ltd", "co", "dr", "mr", "ms", "st", "vol", "ch", "sec", "min",
           "max", "avg", "est")
#
# Masked before splitting rather than excluded by a lookbehind, because a
# variable-width lookbehind is not a legal Python pattern. Under-splitting is the
# safe direction for these checks: a sentence that genuinely ends in "etc." simply
# joins the next one, where over-splitting silently truncates a list.
_ABBREV_RE = re.compile(r"\b(" + "|".join(_ABBREV) + r")\.", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_DOT = "\x00"


def _sentences(text: str) -> list[str]:
    masked = _ABBREV_RE.sub(lambda m: m.group(1) + _DOT, text or "")
    return [s.replace(_DOT, ".").strip()
            for s in _SENTENCE_SPLIT_RE.split(masked) if s.strip()]


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


# The separator between a series noun and its number carries no meaning, and
# treating it as if it did produced a false positive on the first run that priced
# anything. AWS usage types are written closed up — `Requests-Tier1`,
# `Requests-Tier2` — and an agent quoting them as "Tier 1" and "Tier 2" was
# reported for inventing a series that its own evidence had handed it. `_SERIES_RE`
# already tolerates the gap when DETECTING a label, so tolerating it when looking
# the label UP is what makes the two halves agree.
_SERIES_SEP_RE = re.compile(
    r"\b(" + "|".join(_SERIES_NOUNS) + r")s?[\s_#-]*(\d{1,2})\b", re.IGNORECASE)


def _canon_series(text: str) -> str:
    """Comparison form for series labels: `Tier-1`, `Tier 1` and `Tier1` alike."""
    return _SERIES_SEP_RE.sub(lambda m: f"{m.group(1).lower()}{m.group(2)}",
                              _norm(text))


# Nouns that name the PARTS OF A STRUCTURE. A compound count is grounded when the
# inputs already decompose the subject into that many parts, whatever they called
# them: an output saying "four stages" over upstream that says "four-layer
# architecture" and names ingestion, processing, storage and consumption has
# COUNTED its evidence and relabelled it, not invented a plan. Flagged live.
#
# Deliberately narrower than "any noun": upstream saying "seven services have
# published pricing" must not license "a seven-phase rollout". These words denote
# a decomposition; `services` does not.
_STRUCTURE_NOUNS = (
    "layer", "stage", "phase", "step", "tier", "zone", "component", "part",
    "element", "category", "group", "pillar", "area", "dimension", "section",
    "band", "track", "wave", "round", "quadrant", "axis",
)
_COMPOUND_GROUND_RE = {
    word: re.compile(r"\b" + word + r"[-\s]+(?:" + "|".join(_STRUCTURE_NOUNS)
                     + r")(?:ed|s)?\b", re.IGNORECASE)
    for word in _NUMBER_WORDS
}


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


# How far after the noun the list may start. Enough for "stages:", "the following
# stages are:" or a line break, and short enough that a noun mentioned in one
# paragraph cannot be grounded by an unrelated numbered list further down.
_ENUM_INTRO_WINDOW = 120
# A plain list marker: "1. ", "2) ". The trailing whitespace requirement is what
# keeps decimals out — an AWS rate like "0.000005 USD per Request" must not read as
# item 0 of a series.
_ENUM_ITEM_RE = re.compile(r"\b(\d{1,2})[.)]\s")
# Largest gap between one item and the next that still reads as the same list.
# Generous for a wordy item, tight enough that the next numbered list in the same
# evidence block does not extend this one.
_ENUM_GAP = 600


def _upstream_enumeration_size(upstream: str, noun: str) -> int:
    """How many parts a PLAIN ENUMERATION in the inputs breaks `noun` into.

    The third way evidence defines a decomposition, and the one the compound check
    could not see. AWS documentation writes:

        A typical analytics pipeline has the following stages:
        1. Collect data
        2. Store the data
        3. Process the data
        4. Analyze and visualize the data

    An agent reporting "a consistent four-stage pipeline: collect, store, process,
    and analyze/visualize" has COUNTED that list, not invented a plan — but the
    other two escapes both miss it. `_upstream_series_size` looks for the labels
    `Stage 1 … Stage 4`, which a bare "1." is not, and `_COMPOUND_GROUND_RE` needs
    the literal words "four stages", which never appear: the source states the
    count only by enumerating. So the rule told a research agent that a faithful
    summary of its own evidence was invented. Flagged live on
    "build analytics applicaiton on AWS".

    Only a run starting at 1 counts, and only one that begins within
    `_ENUM_INTRO_WINDOW` of the noun, so this grounds a list the inputs actually
    introduce with that noun and not any numbered list that happens to be nearby.
    """
    if not upstream:
        return 0
    best = 0
    for m in re.finditer(r"\b" + noun + r"s?\b", upstream, re.IGNORECASE):
        tail = upstream[m.end():m.end() + _ENUM_INTRO_WINDOW]
        first = _ENUM_ITEM_RE.search(tail)
        if not first or int(first.group(1)) != 1:
            continue
        # Walk the run from where item 1 sits, and STOP at the first marker that is
        # not the next number. Skipping mismatches instead would let the count hop
        # across unrelated lists: these evidence blocks carry several, including the
        # instruction list appended to every research prompt, and a permissive walk
        # read one four-item list as nine — which would have grounded "nine stages"
        # that nothing defines. A run also ends if the next item is too far away to
        # belong to the same list.
        span = upstream[m.end() + first.start():]
        want, end_of_prev = 1, 0
        for item in _ENUM_ITEM_RE.finditer(span):
            if int(item.group(1)) != want or item.start() - end_of_prev > _ENUM_GAP:
                break
            want += 1
            end_of_prev = item.end()
        best = max(best, want - 1)
    return best


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
    # Separator-insensitive, so a label the inputs wrote closed up still counts as
    # grounded when the output spaces it out. See _canon_series.
    canon_series = _canon_series(upstream)
    out: list[Violation] = []

    # 1. Labelled series: group by noun, a series needs at least two members.
    by_noun: dict[str, set[int]] = {}
    for noun, num in _SERIES_RE.findall(prose):
        by_noun.setdefault(noun.lower(), set()).add(int(num))
    for noun, nums in sorted(by_noun.items()):
        if len(nums) < 2:
            continue
        ungrounded = sorted(n for n in nums
                            if not _upstream_has(canon_series, f"{noun}{n}"))
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
        if _upstream_enumeration_size(upstream, noun) >= stated:
            continue  # the inputs enumerate that many parts under this noun
        ground = _COMPOUND_GROUND_RE.get(word.lower())
        if ground and ground.search(upstream or ""):
            continue  # the inputs decompose it into this many parts, under another name
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


# A digit belonging to a LABEL is an identifier, not a quantity. Live false
# positive: "0.000005 USD per Tier-1 Request, 0.0000004 USD per Tier-2 Request"
# reported "1 Request" and "2 Request" as ungrounded figures — the hyphen in
# `Tier-1` opens a word boundary, and `request` is a unit, so the pattern read a
# tier label as a count of requests. The upstream said `Requests-Tier1`, so the
# figures were grounded too; nothing about them was a quantity at all.
#
# This is the same lesson as `_FIGURE_RE` requiring a unit marker in the first
# place (a year, a version, an index are identifiers) and the same lesson as
# `_canon_series` — carried into the rule that missed it.
_LABEL_BEFORE_RE = re.compile(
    r"\b(?:" + "|".join((*_SERIES_NOUNS, "gen", "generation", "version", "v",
                         "type", "class", "level", "group", "part", "table",
                         "figure", "appendix"))
    + r")s?\s*[-\u2010-\u2015]?\s*$", re.IGNORECASE)


# A RANGE WHOSE ENDPOINTS ARE BOTH GROUNDED is a summary of two real figures, not a
# third invented one. Live false positive: the cost asset published Glue at 0.29 USD
# per DPU-Hour and 0.44 USD per DPU-Hour, and a recommendation quoting the spread as
# "0.29–0.44 USD" was reported for inventing a figure. Compressing two rates a reader
# has into the span between them is honest writing, and the alternative — repeating
# every rate individually — is the padding another rule exists to stop.
#
# Both endpoints must appear, with the unit. A range with one real end and one
# invented ("0.29–5.00 USD") still fires, which is the case worth catching.
_RANGE_RE = re.compile(
    r"^([$£€]?\s?\d[\d,]*(?:\.\d+)?)\s*[-\u2010-\u2015\u2212]\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(.*)$")


def _range_is_grounded(token: str, canon_upstream: str) -> bool:
    """Is this "a to b <unit>" span built from two figures the inputs supplied?"""
    m = _RANGE_RE.match(token.strip())
    if not m:
        return False
    low, high, unit = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not unit:
        return False
    return all(_canon_figure(f"{end} {unit}") in canon_upstream
               for end in (low, high))


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
        clean = _clean(text)
        for match in _FIGURE_RE.finditer(clean):
            token = match.group(0).strip()
            canon = _canon_figure(token)
            if canon in seen or canon in canon_upstream:
                continue
            if _LABEL_BEFORE_RE.search(clean[max(0, match.start() - 24):match.start()]):
                continue  # "Tier-1 Request" is a label, not one request
            if _range_is_grounded(token, canon_upstream):
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


# The reader-facing prose fields, checked alongside findings/claims. Deliberately
# NOT `sections` (see the docstring) and not `dataLimitations`/`limitations`, which
# are where a gap statement is supposed to live.
_SUMMARY_KEYS = ("executiveSummary", "summary", "rationale")

# In a summary the brief must be the SENTENCE'S SUBJECT to count, which in practice
# means it appears at the front. Matching it anywhere flagged "Web evidence provides
# current architectural patterns … that address the brief's scope of architecture,
# agent capabilities, tool integration and deployment" — a sentence whose subject is
# the evidence, mentioning the brief only to say what the evidence covers. That is
# at worst padding, and not what this rule is for. The window admits a leading
# connective, so "However, the request brief lacks critical specificity…" still
# fires; that one was the whole first line of a live analysis.
_SUBJECT_WINDOW = 60


def _check_findings_about_brief(payload: dict) -> list[Violation]:
    """A finding or claim whose subject is the brief instead of the evidence.

    Every downstream agent already has the brief. Restating it back as a research
    finding spends one of the few slots a reviewer reads on something that
    carries no new information — observed filling two of five findings, and two
    of six in another agent the same run. `dataLimitations` is the right home for
    "the brief does not specify X".

    THE SUMMARY FIELDS COUNT TOO. Guarding only findings/claims left the sentence
    with the widest audience unguarded, and it showed: an analysis and a
    recommendation both opened their executiveSummary with "the request brief lacks
    critical specificity on use case, autonomy level…", which is the whole defect
    sitting in the one line an approver is guaranteed to read. Not the report's
    `sections`, though — a report section whose subject IS the open questions is
    doing its job, and the report schema has no limitations list to move them to.
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
    for key in _SUMMARY_KEYS:
        for sentence in _sentences(_clean(str(payload.get(key) or ""))):
            match = _BRIEF_REF_RE.search(sentence)
            if match and match.start() < _SUBJECT_WINDOW:
                out.append(Violation(
                    "finding-about-the-brief",
                    f"{key} is about the request rather than the subject: "
                    f"{_quote(sentence)}. This is the line a reviewer reads first; "
                    f"spend it on what you found. Move the gap into the "
                    f"limitations list and say what the evidence shows instead."))
    return out


# ---------------------------------------------------------------------------
# count-mismatch
# ---------------------------------------------------------------------------

# Phrase noun -> every list the phrase could plausibly be counting.
#
# SIBLINGS ARE LISTED TOGETHER ON PURPOSE, and the check passes if the stated
# number matches ANY of them. English does not distinguish these the way the
# schema does: a brief carrying six `keyQuestions` and five `openQuestions` is,
# to a reader, a brief that poses questions. An agent wrote "the six open
# questions identified in the request brief" and then enumerated the six
# keyQuestions verbatim — the count was correct about a real six-item list, and
# the rule called it a mismatch because the noun it chose resolved to the other
# list. The check cannot know which list the prose meant, so when the number
# matches one of them it has no grounds to claim a contradiction.
#
# What it still catches is the case it was built for: a number that matches
# NOTHING the agent is holding, sitting next to the list it contradicts.
_COUNT_TARGETS: dict[str, tuple[str, ...]] = {
    "open questions": ("openQuestions", "keyQuestions"),
    "key questions": ("keyQuestions", "openQuestions"),
    "questions": ("openQuestions", "keyQuestions"),
    "data limitations": ("dataLimitations", "limitations"),
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
        # EVERY list the phrase could be counting, not just the first that
        # resolves. The stated number only contradicts the output if it matches
        # none of them — see _COUNT_TARGETS for the sibling-list false positive
        # this prevents.
        candidates: dict[str, int] = {}
        for key in _COUNT_TARGETS[noun_l]:
            if isinstance(payload.get(key), list):
                candidates[key] = len(payload[key])
            elif key in extra_counts:
                candidates[key] = extra_counts[key]
        if not candidates or stated in candidates.values():
            continue
        if (noun_l, stated) in seen:
            continue
        seen.add((noun_l, stated))
        sizes = " or ".join(str(n) for n in dict.fromkeys(candidates.values()))
        out.append(Violation(
            "count-mismatch",
            f'the output says "{raw_n} {middle}{noun}" but there are {sizes}. State '
            f"the count that matches the items you actually list, or omit the "
            f"count."))
    return out


# THE INLINE-ENUMERATION CHECK USED TO LIVE HERE, AND WAS REMOVED. The record
# matters more than the code did.
#
# It compared a stated count against the list in the same sentence — "nine core
# best practices: <eight items>" — which `_check_counts` cannot see, because "best
# practices" is not a schema list. It caught two real defects, each of which had
# propagated through three or four assets: that one, and "five functional layers"
# followed by four of them.
#
# It also produced EIGHT distinct false-positive shapes on real output in a single
# day, each needing its own narrowing: a conjunction inside a clause, a narrowing
# word inside the noun capture, an abbreviation ("Lambda vs. Glue") truncating the
# sentence, a verb between the count and the colon, a nested sub-list after a dash,
# long clauses with a subset quoted, compound items over-split, and finally a
# trailing participial clause counted as an item ("five logical layers: event
# trigger, orchestration, service, data, consumption, enabling independent scaling"
# — five layers named, six items found). On its last run BOTH firings were false
# and neither was a real defect.
#
# The pattern is the lesson: deciding what a colon governs, and where one list item
# ends and the next begins, is parsing English, and each narrowing moved the rule
# closer to matching only its own fixtures. A check nobody can trust is worse than
# no check, because a reviewer learns to skip the whole panel.
#
# `_check_counts` remains and is the reliable half: it compares a stated count
# against a LIST THE PAYLOAD ACTUALLY HAS, where there is no parsing to get wrong.
# If the inline case needs covering again, cover it in the prompts, or with a schema
# that puts the enumeration in a field instead of in prose.



# ---------------------------------------------------------------------------
# over-budget
# ---------------------------------------------------------------------------
#
# THE ONLY RULE WHOSE THRESHOLD IS CONFIG, because length is the only defect here
# that has no correct value a framework could know. Every other check tests a
# payload against its own inputs; this one tests it against a decision the workflow
# owner makes, so the number lives in `outputRules.maxWords` in workflow.json and
# defaults to 0, meaning off.
#
# It exists because prompting failed. A report agent was told not to transcribe its
# upstream, then told that compressing is not numbering, then told to keep sections
# to what a reviewer will read — and went from 2164 words to 1749 to 2600, ending
# with an ordinal sequence it had also been told not to invent. Wording is
# negotiable and a number is not.
#
# Counted over the prose the reader sees, via the same `_walk_content` every other
# check uses, so it follows the schema rather than naming any agent's fields.


_INDEXED_RE = re.compile(r"^(\w+)\[(\d+)\]")


def _named(payload: dict, path: str) -> str:
    """`sections[1].content` -> `sections[1](recommendations)`.

    An index alone makes a reviewer — or a re-asked model — count list entries to
    find the one being talked about. Where the entry carries a name, say it. Generic
    over the schema: any list whose entries have a `sectionType`, `title` or `name`.
    """
    m = _INDEXED_RE.match(path)
    if not m:
        return path
    entries = payload.get(m.group(1))
    if not isinstance(entries, list):
        return path
    i = int(m.group(2))
    if i >= len(entries) or not isinstance(entries[i], dict):
        return path
    for key in ("sectionType", "title", "name"):
        label = str(entries[i].get(key) or "").strip()
        if label:
            return f"{m.group(1)}[{i}]({label[:48]})"
    return path


def prose_words(payload: dict) -> int:
    """Words of reader-facing prose in a payload. Public because the repair loop
    needs it to break a tie: two answers with one `over-budget` violation each are
    not equally good if one is a third shorter."""
    if not isinstance(payload, dict):
        return 0
    return sum(len(text.split()) for _p, text in _walk_content(payload))


def _check_budget(payload: dict, max_words: int) -> list[Violation]:
    """The payload's prose measured against the budget its config sets."""
    if not max_words or max_words < 0:
        return []
    counts = [(_named(payload, path), len(text.split()))
              for path, text in _walk_content(payload)]
    total = sum(n for _p, n in counts)
    if total <= max_words:
        return []
    worst = sorted(counts, key=lambda c: -c[1])[:3]
    where = ", ".join(f"{p} ({n} words)" for p, n in worst if n)
    return [Violation(
        "over-budget",
        f"the output runs to {total} words against a budget of {max_words}. "
        f"Longest: {where}. Cut it to the budget by saying each thing once and "
        f"leaving out what the upstream assets already record — do not drop a "
        f"section, and do not compress by numbering the content.")]


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
_FETCH_VERBS = (
    "attempt to retrieve", "retrieve", "retrieving", "consult", "consulting",
    "obtain", "obtaining", "examine", "examining", "reuse", "reusing", "refer to",
    "referring to", "review", "reviewing", "gather", "gathering", "collect",
    "collecting",
)
_FETCH_RE = re.compile(r"\b(?:" + "|".join(_FETCH_VERBS) + r")\b", re.IGNORECASE)

# A CONDITIONAL GAP STATEMENT: "obtaining this would enable X", "having those
# artifacts would unblock Y". This is not an action — it is the remedy this rule
# ASKS FOR in its own message ("state what is missing and what having it would
# unblock"). Firing on it punished the exact wording the repair instruction
# requested, which is how a report that had correctly rewritten its next-steps
# section into pure gap disclosure was still reported as breaking the rule.
#
# The tell is the modal: a step tells the reader to do something, whereas a gap
# statement describes what a thing the reader does NOT have would make possible.
_CONDITIONAL_GAP_RE = re.compile(
    r"\b(?:" + "|".join((*_FETCH_VERBS, "having", "closing", "resolving",
                         "specifying", "providing", "clarifying", "answering"))
    + r")\b[^.;:!?]{0,120}?"
    r"\b(?:would|could|will)\s+(?:enable|unblock|allow|let|accelerate|permit|"
    r"inform|support|make)\b",
    re.IGNORECASE)


# THE DISTINCTION THIS RULE HAS TO DRAW, and did not.
#
# An ARTIFACT that is unavailable cannot be fetched: "Consult the artifacts from
# the four prior runs" beside "those artifacts are not available" hands the reader
# an action they cannot take. That is the defect, and it still is.
#
# A REQUIREMENT that is unspecified is different in kind. "The source type is not
# provided" beside "Obtain and document: (1) data source type, (2) expected data
# volume…" is not a dead end — it is a person answering a question, and on a run
# where the workload was never described it is the single most useful thing the
# deliverable can say. Flagged live, and suppressing it to satisfy the check would
# have let the check damage the report it exists to protect.
#
# The tell is the noun. `volume`, `latency`, `budget`, `use case` are things a
# requester supplies; `artifact`, `output`, `record`, `log` are things a system
# either has or does not.
_REQUIREMENT_NOUNS = (
    "requirement", "specification", "spec", "volume", "velocity", "throughput",
    "latency", "budget", "constraint", "use case", "profile", "scale", "timeline",
    "deadline", "source type", "data source", "sla", "target", "preference",
    "criteria", "objective", "scope", "pattern", "workload",
)
_ARTIFACT_NOUNS = (
    "artifact", "output", "document", "record", "report", "evidence", "file",
    "log", "asset", "dataset", "transcript", "attachment", "snapshot",
)
_REQUIREMENT_RE = re.compile(r"\b(?:" + "|".join(_REQUIREMENT_NOUNS) + r")s?\b",
                             re.IGNORECASE)
_ARTIFACT_RE = re.compile(r"\b(?:" + "|".join(_ARTIFACT_NOUNS) + r")s?\b",
                          re.IGNORECASE)
# How far back to look for the noun the unavailability statement is about.
_SUBJECT_LOOKBEHIND = 90
# How far either side of a gap statement a fetch verb still counts as the same
# statement. Wide enough to span an item's `detail` and its `rationale`, which is the
# case the rule was built for; far short of a report section.
_PAIR_WINDOW = 320


def _about_a_requirement(text: str, at: int) -> bool:
    """Is the thing said to be missing an input a requester supplies?

    Judged on the clause leading up to the statement, not the whole entry: an item
    may legitimately mention artifacts elsewhere. When BOTH kinds of noun appear the
    artifact wins, because a fetch aimed at one is the defect and a false negative
    here is cheaper than re-breaking the case the rule was built for.
    """
    window = text[max(0, at - _SUBJECT_LOOKBEHIND):at]
    if _ARTIFACT_RE.search(window):
        return False
    return bool(_REQUIREMENT_RE.search(window))


def _check_action_on_unavailable(payload: dict) -> list[Violation]:
    """An action item that tells the reader to fetch something it says is missing.

    Observed in both forms: the blunt "Consult the artifacts from the four prior
    runs" alongside "those artifacts are not available", and — after the prompt
    banned that — the hedged "Attempt to retrieve prior run artifacts, which may
    not be retrievable". Naming the same gap as both a step and its own caveat is
    the defect either way: the reader is handed an action they cannot take.

    Scoped to action-bearing fields only. A `risks` or `limitations` entry saying
    something is unavailable is exactly right, and must not be penalised.

    Matched per ENTRY, not per sentence: an item's `detail` says "consult the prior
    artifacts" while its `rationale` says "those are not available", and to a reader
    that is one statement. Splitting them apart missed the original defect.

    But a CONDITIONAL GAP STATEMENT does not count as the action half. A live false
    positive named a gap in one sentence ("their outputs are not available") and
    then wrote, three sentences later, "retrieving those assets would accelerate the
    effort" — a description of what the missing thing would be worth, which is the
    remedy this rule's own message asks for. Those spans are removed before looking
    for a fetch verb, so the rule keeps its reach across an entry without punishing
    the wording it recommends.
    """
    out: list[Violation] = []
    for label, text in _action_items(payload):
        # What is left after the "having X would unblock Y" clauses are removed.
        actionable = _CONDITIONAL_GAP_RE.sub(" ", text)
        for unavailable in _UNAVAILABLE_RE.finditer(text):
            # An UNSPECIFIED REQUIREMENT is not unavailable EVIDENCE, and telling a
            # reader to go and get one is the opposite of a dead end.
            if _about_a_requirement(text, unavailable.start()):
                continue
            # THE TWO HALVES HAVE TO BE ABOUT THE SAME THING, and proximity is the
            # only evidence of that available here. The rule reaches across an entry
            # on purpose — a recommendation item states the action in `detail` and the
            # gap in `rationale`, two sentences apart, and to a reader that is one
            # statement. A REPORT SECTION is also one entry and runs to a thousand
            # words, which paired "private pricing and data transfer between services
            # are not included" with "Obtain from the requester: (1) data type…" from
            # a different paragraph on a different subject. Same entry, unrelated
            # sentences, and the rule read them as one contradiction.
            near = actionable[max(0, unavailable.start() - _PAIR_WINDOW):
                              unavailable.end() + _PAIR_WINDOW]
            fetch = _FETCH_RE.search(near)
            if not fetch:
                continue
            out.append(Violation(
                "action-on-unavailable",
                f"{label} recommends obtaining something the same entry says is "
                f"unavailable: {_quote(near)}. An action the reader cannot take is "
                f"not a step. State what is missing and what having it would "
                f"unblock, in the limitations, and drop the action."))
            break
    return out


# ---------------------------------------------------------------------------
# Profiles + entry point
# ---------------------------------------------------------------------------

OVER_BUDGET = "over-budget"
INVENTED_NUMBERING = "invented-numbering"
UNSUPPORTED_FIGURE = "unsupported-figure"
ABOUT_REQUEST = "about-the-request"
FINDING_ABOUT_BRIEF = "finding-about-the-brief"
COUNT_MISMATCH = "count-mismatch"
DUPLICATE_IN_LIST = "duplicate-in-list"
ACTION_ON_UNAVAILABLE = "action-on-unavailable"

ALL_RULES = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, ABOUT_REQUEST, FINDING_ABOUT_BRIEF,
    COUNT_MISMATCH, DUPLICATE_IN_LIST, ACTION_ON_UNAVAILABLE, OVER_BUDGET,
})

# A research agent's subject may legitimately BE this system or its own run
# history, so ABOUT_REQUEST is off here — an agent whose job was reporting on prior
# runs is the case that established it. Its own observed defect — restating the
# brief as a finding — is covered.
RESEARCH = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, FINDING_ABOUT_BRIEF,
    COUNT_MISMATCH, DUPLICATE_IN_LIST, OVER_BUDGET,
})

# The three synthesis agents produce the reader-facing deliverable, so every rule
# applies, including the two about what a deliverable may be ABOUT.
SYNTHESIS = ALL_RULES

# The first agent has no upstream assets — its "upstream" is the raw request — so
# the rules that need a grounding baseline still work, but there is no brief to
# restate and nothing yet to recommend.
BRIEF = frozenset({
    INVENTED_NUMBERING, UNSUPPORTED_FIGURE, COUNT_MISMATCH, DUPLICATE_IN_LIST,
    OVER_BUDGET,
})


def check(payload: dict, *, upstream: str = "", rules=SYNTHESIS,
          extra_counts: dict[str, int] | None = None,
          max_words: int = 0) -> list[Violation]:
    """Every violation in `payload`, given the `upstream` text the model was shown.

    `rules` selects which checks apply (see the profiles above). `max_words` is the
    prose budget from this agent's `outputRules.maxWords`; 0 means no budget, which
    is the default, because the right length is a property of the workflow and not
    of the framework. Never raises and never mutates: an unparseable or empty
    payload simply has no violations, because that case is already handled by the
    runners' degrade path.
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
        counts = _check_counts(payload, extra_counts)
        found += counts
    if DUPLICATE_IN_LIST in rules:
        found += _check_duplicates(payload)
    if ACTION_ON_UNAVAILABLE in rules:
        found += _check_action_on_unavailable(payload)
    if OVER_BUDGET in rules:
        found += _check_budget(payload, max_words)
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
