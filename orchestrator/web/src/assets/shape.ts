/** How an asset is READ, with no knowledge of any particular contract.
 *
 *  This is the file that makes the framework's central promise true on the UI side: a
 *  customer defines their own asset type, with their own field names, and it still
 *  gets first-class layout. Everything here decides by SHAPE — is this a list of
 *  objects that look like claims? is this key one that names an uncertainty? — and
 *  never by this sample's field names.
 *
 *  Ported from the pre-Cloudscape page. The behaviour is asserted in shape.test.ts
 *  against a contract the framework has never seen. */

/** The envelope the FRAMEWORK owns (app/common/contracts/base.py). Everything else in
 *  an asset is the customer's, so these are the only names this file may know. */
export const ENVELOPE = new Set([
  "assetId", "assetType", "version", "status", "createdAt", "createdByAgent",
  "sourceAssetIds", "artifacts", "sources",
]);

/** A key whose value is a list of things the run could NOT settle. Matched on generic
 *  English rather than on `openQuestions`/`limitations`/`dataLimitations`, so a
 *  workflow calling it `caveats`, `gaps` or `unknowns` is rendered the same way. */
export const UNCERTAIN_KEY =
  /question|limitation|gap|unknown|caveat|missing|unresolved|outstanding|risk|blocker|assumption|exclusion|constraint/i;

/** The evidence classifications the research contract uses. Recognised for COLOUR
 *  only — an unrecognised value still renders, as a neutral badge. */
export const KNOWN_CLASSIFICATIONS = new Set([
  "sourced-fact", "calculation", "assumption", "agent-interpretation",
]);

export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export function isObject(v: unknown): v is Record<string, Json> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Parse an agent's output. Returns null when it is not an object — a plain-text agent
 *  is legitimate, and its output is shown as prose rather than forced into a shape. */
export function parseAsset(text: string): Record<string, Json> | null {
  if (!text) return null;
  try {
    const v: unknown = JSON.parse(text);
    return isObject(v) ? v : null;
  } catch {
    return null;
  }
}

/** `camelCase` / `snake_case` → "Camel case". Used for any key this code has never
 *  seen, which is most of them. */
export function humanizeKey(k: string): string {
  const words = k
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return k;
  // SENTENCE CASE, which is the AWS convention: only the first word is capitalised.
  // Title Case ("Settlement Gbp", "Max Tokens") reads like a product name rather than
  // a field label, and Cloudscape's own labels are sentence case throughout.
  return words
    .map((w, i) => (i === 0
      ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
      : w.toLowerCase()))
    .join(" ");
}

/** Does this list look like CLAIMS — objects carrying a statement plus provenance?
 *  Decided on the shape of the first entry, so `claims`, `findings`, `items` and a
 *  name nobody has thought of all get the same treatment. */
export function isClaimList(v: unknown): v is Array<Record<string, Json>> {
  if (!Array.isArray(v) || v.length === 0) return false;
  const first = v[0];
  if (!isObject(first)) return false;
  const keys = Object.keys(first);
  const hasText = keys.some((k) => /statement|claim|title|text|finding|detail|summary/i.test(k));
  const hasMeta = keys.some((k) =>
    /classification|confidence|priority|sourceref|tracedto|source|severity|rationale/i.test(k));
  return hasText && hasMeta;
}

/** A short lower-case token — `high`, `sourced-fact`, `in-review`. Rendered as a badge
 *  rather than as prose, which is what makes an unfamiliar contract still look
 *  designed. Deliberately narrow: anything with a space is prose. */
export function isVocabToken(s: unknown): boolean {
  return typeof s === "string"
    && s.length > 0 && s.length <= 28
    && /^[a-z0-9][a-z0-9._-]*$/.test(s);
}

/** Only http(s), and only when it parses. A model that invents a `javascript:` URL
 *  must not become a live link. */
export function safeUrl(u: unknown): string | null {
  if (typeof u !== "string") return null;
  try {
    const p = new URL(u);
    return p.protocol === "http:" || p.protocol === "https:" ? u : null;
  } catch {
    return null;
  }
}

/** Pull the one-line gist, if the asset has one. Tried in the order a reader would
 *  want it, and absent is a normal answer. */
export function gistOf(a: Record<string, Json>): string | null {
  for (const k of ["executiveSummary", "summary", "title"]) {
    const v = a[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

export interface Section {
  key: string;
  label: string;
  value: Json;
  /** A list of claim-like objects gets the richer layout. */
  claims: boolean;
  /** An uncertainty list is shown last and styled as a caveat. */
  uncertain: boolean;
}

/** Split an asset into the sections to render, in reading order: the gist first (handled
 *  separately), then substance, then what could not be settled, with the envelope's
 *  bookkeeping dropped entirely — a reviewer does not need `createdAt` in the body. */
export function sectionsOf(a: Record<string, Json>): Section[] {
  const gistKeys = new Set(["executiveSummary", "summary", "title"]);
  const out: Section[] = [];
  for (const [key, value] of Object.entries(a)) {
    if (ENVELOPE.has(key) || gistKeys.has(key)) continue;
    if (value == null) continue;
    if (Array.isArray(value) && value.length === 0) continue;
    if (isObject(value) && Object.keys(value).length === 0) continue;
    if (typeof value === "string" && !value.trim()) continue;
    out.push({
      key,
      label: humanizeKey(key),
      value,
      claims: isClaimList(value),
      uncertain: UNCERTAIN_KEY.test(key),
    });
  }
  // Uncertainty last: it is context for everything above it.
  return [...out.filter((s) => !s.uncertain), ...out.filter((s) => s.uncertain)];
}

/** The fields of a claim-like object, split into the parts the layout treats
 *  differently. Nothing is dropped — an unrecognised field becomes a detail row. */
export interface ClaimParts {
  text: string | null;
  badges: Array<{ key: string; value: string }>;
  refs: string[];
  rest: Array<{ key: string; label: string; value: Json }>;
}

export function claimParts(item: Record<string, Json>): ClaimParts {
  let text: string | null = null;
  const badges: ClaimParts["badges"] = [];
  const refs: string[] = [];
  const rest: ClaimParts["rest"] = [];
  for (const [k, v] of Object.entries(item)) {
    if (v == null || (Array.isArray(v) && v.length === 0)) continue;
    if (text === null && typeof v === "string"
        && /statement|claim|title|text|finding|detail/i.test(k)) {
      text = v;
      continue;
    }
    if (typeof v === "string" && isVocabToken(v)) {
      badges.push({ key: k, value: v });
      continue;
    }
    if (/tracedto|sourceasset|sourceids?$/i.test(k) && Array.isArray(v)) {
      refs.push(...v.filter((x): x is string => typeof x === "string"));
      continue;
    }
    if (typeof v === "string" && /sourceref|reference/i.test(k)) {
      refs.push(v);
      continue;
    }
    rest.push({ key: k, label: humanizeKey(k), value: v });
  }
  // A claim-like object with no matching text field still has to show something.
  if (text === null) {
    const firstProse = rest.find(
      (r) => typeof r.value === "string" && (r.value as string).length > 24);
    if (firstProse) {
      text = firstProse.value as string;
      rest.splice(rest.indexOf(firstProse), 1);
    }
  }
  return { text, badges, refs, rest };
}
