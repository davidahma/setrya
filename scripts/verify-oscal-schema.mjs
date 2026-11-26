import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

// The official OSCAL patterns use ECMAScript Unicode properties.
const [prefix, schemas, plan, ...results] = process.argv.slice(2);
if (!prefix || !schemas || !plan || results.length === 0) {
  throw new Error("Expected the npm prefix, NIST schema directory, Assessment Plan and Assessment Results.");
}
const require = createRequire(resolve(prefix, "package.json"));
const Ajv = require("ajv");
const addFormats = require("ajv-formats");
const ajv = new Ajv({ strict: false });
addFormats(ajv);

for (const [name, expectedHash, documents] of [
  ["plan", "0c83b6b85f8d06c786feb5dd3bdafb44dcf5c0feff4f47ca5cb95cf5379438a5", [plan]],
  ["results", "a587b1580651f435a376d04dde78aaba6783c58ceb6f93f482d0b61d0c8afa59", results],
]) {
  const schema = readFileSync(join(schemas, `oscal_assessment-${name}_schema.json`));
  if (createHash("sha256").update(schema).digest("hex") !== expectedHash) {
    throw new Error(`Official OSCAL 1.2.0 assessment-${name} schema fingerprint differs.`);
  }
  const validate = ajv.compile(JSON.parse(schema));
  for (const document of documents) {
    if (!validate(JSON.parse(readFileSync(document, "utf8")))) {
      throw new Error(`OSCAL assessment-${name} failed schema validation: ${ajv.errorsText(validate.errors)}`);
    }
    console.log(`Official OSCAL 1.2.0 assessment-${name} schema passed.`);
  }
}
