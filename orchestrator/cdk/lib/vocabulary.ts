/**
 * The framework's closed value sets, read from app/vocabulary.json.
 *
 * WHY THIS READS A FILE instead of declaring the arrays here. Every one of these lists
 * used to be written out two or three times — once in Python, once in this directory,
 * and once in terraform/*.tf. `toolTypes` and the RBAC action names existed in all
 * three.
 *
 * That cost was real and it caused real bugs: adding a value meant finding every copy,
 * and a copy that got missed rejected a config the other planes accepted, so whether a
 * workflow deployed depended on which IaC path you used. A parity test existed purely
 * to compare the copies against one another — a test that a duplicate has not drifted,
 * rather than a reason for the duplicate to exist.
 *
 * JSON is the one format all three planes read natively, so the list is held once.
 * `cdk/test/parity.test.ts` now asserts that each plane READS this file.
 */
import * as fs from "fs";
import * as path from "path";

const VOCAB_PATH = path.join(__dirname, "..", "..", "app", "vocabulary.json");

type VocabFile = Record<string, { values?: string[] }>;

const RAW: VocabFile = JSON.parse(fs.readFileSync(VOCAB_PATH, "utf8"));

/**
 * The closed set called `name`. Throws when it is not declared rather than returning
 * `[]` — an empty set would make every value invalid, or every value valid, depending
 * on how the caller uses it, and neither is a good silent default.
 */
export function values(name: string): string[] {
  const block = RAW[name];
  if (!block || !Array.isArray(block.values)) {
    throw new Error(
      `"${name}" is not a vocabulary in app/vocabulary.json. Declared: ` +
        Object.keys(RAW)
          .filter((k) => !k.startsWith("$"))
          .sort()
          .join(", ")
    );
  }
  return block.values;
}

// Named bindings, so a reader sees the vocabulary at the point of use rather than a
// string lookup, and a typo is a compile error rather than a throw at synth.
export const RUNTIMES = values("runtimes");
export const TOOL_TYPES = values("toolTypes");
export const TOOL_SCHEMA_PROPERTY_TYPES = values("toolSchemaPropertyTypes");
export const TOOL_AUTH_MODES = values("toolAuthModes");
export const API_KEY_TOOL_TYPES = values("apiKeyToolTypes");
export const TOOL_LISTING_MODES = values("toolListingModes");
export const A2A_AUTH_MODES = values("a2aAuthModes");
export const A2A_SOURCES = values("a2aSources");
export const A2A_LAMBDA_SKILLS = values("a2aLambdaSkills");
export const MEMORY_STRATEGIES = values("memoryStrategies");
export const AUTHORIZATION_ACTIONS = values("authorizationActions");
export const WEB_SEARCH_REGIONS = values("webSearchRegions");
export const GUARDRAIL_FILTER_STRENGTHS = values("guardrailFilterStrengths");
export const GUARDRAIL_PII_ACTIONS = values("guardrailPiiActions");
export const BUILTIN_LAMBDA_SOURCE = values("builtinLambdaSource")[0];
export const EMBEDDING_MODELS = values("embeddingModels");
export const KB_CORPUS_OPERATORS = values("kbCorpusOperators");

/**
 * The embedding dimensions a model supports, FIRST being its default.
 *
 * A second shape in the same file, because this one is a map rather than a list: the
 * model and the dimension have to agree, and holding the pairing anywhere else would
 * put it in two planes again (see terraform/kb.tf, which reads the same key). Throws on
 * an unknown model for the same reason `values` does — silently defaulting the dimension
 * of a model nobody validated is how an empty corpus gets deployed successfully.
 */
export function embeddingDimensions(model: string): number[] {
  const dims = (RAW.embeddingModels as any)?.dimensionsByModel?.[model];
  if (!Array.isArray(dims) || !dims.length) {
    throw new Error(
      `"${model}" has no dimensionsByModel entry in app/vocabulary.json. ` +
        `Declared: ${EMBEDDING_MODELS.join(", ")}`
    );
  }
  return dims;
}
