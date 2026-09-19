"""The asset renderer lays out a contract it has never seen.

`web/index.html` is the one file a customer is told not to edit, and it used to have
THIS SAMPLE's research vocabulary baked into it: `summary`, `findings` and
`dataLimitations` sat in the "envelope" skip-list (they are not envelope fields — see
`AssetEnvelope` in app/common/contracts/base.py), and two hardcoded blocks read
`classification` / `statement` / `sourceRef`. So a customer whose contract says
`observations` and `unresolvedQueries` got their content rendered as a generic
key/value afterthought while three fields they do not have were privileged.

The renderer now selects a layout by the SHAPE of a value. These tests run the real
functions out of index.html under node and assert on the HTML, because the claim is
about what a reader actually sees — the alternative is grepping for strings, which is
how the old coupling survived so long.

Skipped when node is unavailable; the rest of the suite does not need it.
"""
import json
import re
import shutil
import subprocess
import textwrap

import pytest
from conftest import ORCH_ROOT

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None,
                                reason="node is needed to run the page's own functions")

# This sample's research asset, as `research.synthesize` emits it.
SAMPLE_ASSET = {
    "assetId": "asset-research-web_search-x-v1",
    "assetType": "research-finding",
    "version": 1,
    "status": "in-review",
    "createdAt": "2026-09-19T01:00:00-04:00",
    "createdByAgent": "web_search",
    "executiveSummary": "Ingestion choice turns on ordering guarantees.",
    "summary": "Ingestion choice turns on ordering guarantees.",
    "findings": [
        {"statement": "Kinesis preserves order per shard.",
         "classification": "sourced-fact",
         "sourceRef": "https://docs.aws.amazon.com/streams/"},
        {"statement": "SQS does not preserve order unless FIFO.",
         "classification": "assumption", "sourceRef": "Developer guide"},
    ],
    "dataLimitations": ["Cold-start latency was not quantified."],
    "sources": [{"sourceId": "s1", "sourceType": "documentation",
                 "sourceName": "Kinesis guide",
                 "url": "https://docs.aws.amazon.com/streams/"}],
}

# A claims pipeline. Same envelope, entirely different business vocabulary, and one
# artifact — which the renderer used to drop on the floor.
FOREIGN_ASSET = {
    "assetId": "asset-settlement-88213-v2",
    "assetType": "claim-decision",
    "version": 2,
    "status": "approved",
    "createdAt": "2026-09-19T01:00:00-04:00",
    "createdByAgent": "adjuster",
    "executiveSummary": "Claim 88213 is payable in part.",
    "observations": [
        {"observation": "The roof damage predates the policy inception date.",
         "confidence": "high",
         "evidenceUrl": "https://claims.example/88213/photo-3"},
        {"observation": "Water ingress is consistent with the reported storm.",
         "confidence": "medium", "adjusterNote": "Corroborated by the site visit."},
    ],
    "settlementBand": {"currency": "GBP", "low": 4200, "high": 6100},
    "unresolvedQueries": ["The site photographs for the east elevation were not supplied."],
    "artifacts": [
        {"artifactId": "a1", "name": "Signed settlement form",
         "artifactType": "claim-form", "artifactRole": "final",
         "uri": "https://claims.example/88213/form.pdf",
         "description": "Countersigned 18 September"},
    ],
    "sources": [{"sourceId": "s1", "sourceType": "claim-file",
                 "sourceName": "Claim 88213 file"}],
}


def _render(asset: dict) -> str:
    """Run the page's own renderAssetHtml over `asset` and return the HTML.

    The functions are sliced out of index.html rather than duplicated here, so this
    tests the shipped code. `esc` and `mdInline` are defined further down the file and
    are pulled in separately.
    """
    page = (ORCH_ROOT / "web" / "index.html").read_text()

    start = page.index("// --- Structured asset view")
    end = page.index("/* Where an agent gets its data")
    block = page[start:end]

    helpers = "\n".join(
        m.group(0) for m in re.finditer(
            r"^function (?:esc|mdInline)\b.*?^}", page, re.DOTALL | re.MULTILINE))
    assert "function esc" in helpers and "function mdInline" in helpers, (
        "could not find esc/mdInline in index.html — this slice needs updating")

    driver = f"""
{helpers}
{block}
process.stdout.write(renderAssetHtml(JSON.stringify({json.dumps(asset)})));
"""
    # The absolute path comes from shutil.which, so there is no PATH lookup at call
    # time, and the script is built from fixtures in this file.
    out = subprocess.run([_NODE, "-e", textwrap.dedent(driver)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"the renderer threw:\n{out.stderr}"
    return out.stdout


# ---------------------------------------------------------------------------
# A contract the renderer has never seen
# ---------------------------------------------------------------------------

def test_a_foreign_contracts_claim_list_gets_the_same_treatment_as_findings():
    """`observations` is rendered as a claim list, not a key/value grid, because the
    renderer matches on shape. Under the old code only `findings` got this."""
    html = _render(FOREIGN_ASSET)
    assert "<h5>Observations</h5>" in html
    assert "The roof damage predates the policy inception date." in html
    # The short scalar reads as a category and gets the chip the classification had.
    assert 'class="cls cls-high"' in html
    assert 'class="cls cls-medium"' in html
    # A url in a claim is a citation and must be a real link.
    assert 'href="https://claims.example/88213/photo-3"' in html
    # A SENTENCE trails as a reference; it must not become a category chip. With a
    # naive character-count rule this 31-character sentence rendered as
    # `<span class="cls cls-corroborated-by-the-site-visit">`, which is absurd.
    assert '<span class="sref">— Corroborated by the site visit.</span>' in html
    assert "cls-corroborated" not in html


def test_a_foreign_contracts_uncertainty_list_is_collapsed_and_flagged():
    """`unresolvedQueries` gets what `dataLimitations` used to get by name, via the
    same generic word list app/common/context.py already uses for memory."""
    html = _render(FOREIGN_ASSET)
    assert "<details" in html and "asset-sec warn" in html
    assert "Unresolved Queries (1)" in html
    assert "east elevation" in html


def test_artifacts_are_rendered_at_all():
    """They were in the envelope skip-list and displayed NOWHERE, so a customer
    attaching a signed form, a spreadsheet or a scan saw no trace of it."""
    html = _render(FOREIGN_ASSET)
    assert "<h5>Artifacts</h5>" in html
    assert "Signed settlement form" in html
    assert 'href="https://claims.example/88213/form.pdf"' in html
    # artifactType is an open string now, so it is shown as given.
    assert "claim-form" in html
    assert "Countersigned 18 September" in html


def test_a_nested_object_still_gets_the_key_value_grid():
    """Shape routing, not name routing: `settlementBand` is an object, so it gets the
    grid rather than the claim list."""
    html = _render(FOREIGN_ASSET)
    assert "<h5>Settlement Band</h5>" in html
    assert "kvgrid" in html
    assert "4200" in html and "GBP" in html


# ---------------------------------------------------------------------------
# The shipped contract must not regress
# ---------------------------------------------------------------------------

def test_the_samples_findings_still_render_as_a_claim_list():
    html = _render(SAMPLE_ASSET)
    assert "<h5>Findings</h5>" in html
    assert "Kinesis preserves order per shard." in html
    # The evidence-class chips still carry their own colours.
    assert 'class="cls cls-sourced-fact"' in html
    assert 'class="cls cls-assumption"' in html


def test_the_claim_leads_with_its_statement_not_with_its_citation_url():
    """A url is never the claim, it is the evidence for it.

    With a plain "longest string wins" rule, a 36-character citation url outranked the
    34-character statement and the page led with the link.
    """
    html = _render(SAMPLE_ASSET)
    statement = "Kinesis preserves order per shard."
    url = "https://docs.aws.amazon.com/streams/"
    finding = html[html.index("<h5>Findings</h5>"):html.index("<h5>Sources</h5>")]
    assert finding.index(statement) < finding.index(url), (
        "the finding led with its citation url instead of its statement")
    # And the url is still a real link, not bare text.
    assert f'href="{url}"' in finding


def test_a_two_word_citation_is_a_reference_not_a_vocabulary_chip():
    """"Developer guide" is a source, not a category. Allowing two-word chips promoted
    it to one and pushed it in front of the statement it cites."""
    html = _render(SAMPLE_ASSET)
    assert '<span class="sref">— Developer guide</span>' in html
    assert "cls-developer-guide" not in html


def test_the_samples_data_limitations_still_collapse():
    html = _render(SAMPLE_ASSET)
    assert "Data Limitations (1)" in html
    assert "asset-sec warn" in html


def test_a_field_repeating_the_executive_summary_is_not_shown_twice():
    """This sample's research asset carries the same sentence in `executiveSummary`
    and `summary`. `summary` is no longer treated as an envelope field, so without a
    name-agnostic dedup it would render as its own second section."""
    html = _render(SAMPLE_ASSET)
    assert html.count("Ingestion choice turns on ordering guarantees.") == 1
    assert "<h5>Summary</h5>" not in html


def test_sources_still_render_as_links_because_the_terms_require_it():
    """AgentCore Web Search's acceptable-use terms require returned citations and
    links to be displayed wherever a result is surfaced."""
    html = _render(SAMPLE_ASSET)
    assert "<h5>Sources</h5>" in html
    assert 'href="https://docs.aws.amazon.com/streams/"' in html


# ---------------------------------------------------------------------------
# Safety — every value here came from a model
# ---------------------------------------------------------------------------

def test_model_supplied_html_is_escaped():
    html = _render({
        "assetId": "a", "assetType": "x", "version": 1, "status": "final",
        "createdAt": "t", "createdByAgent": "a",
        "observations": [{"observation": "<img src=x onerror=alert(1)>",
                          "confidence": "high"}],
    })
    assert "<img" not in html
    assert "onerror" not in html or "&lt;img" in html


def test_a_non_http_url_is_never_emitted_as_a_link():
    """A citation url arrives from a model, so javascript:/data: must not become
    markup."""
    html = _render({
        "assetId": "a", "assetType": "x", "version": 1, "status": "final",
        "createdAt": "t", "createdByAgent": "a",
        "artifacts": [{"artifactId": "a1", "name": "trap",
                       "artifactType": "link", "uri": "javascript:alert(1)"}],
        "observations": [{"observation": "see this", "ref": "data:text/html,<b>x</b>"}],
    })
    assert "<a href=\"javascript:" not in html
    assert "<a href=\"data:" not in html
    assert "trap" in html  # still shown, just not as a link
