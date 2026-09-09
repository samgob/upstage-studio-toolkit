# Editing your schema, classes, and instruct prompt

Your Agent's behavior is defined by three pieces of JSON you fully control. You
can edit them in the Studio UI, or export and edit them as files and load them
back. This guide covers the structure and the rules that keep them valid.

A quick note on how configs version: each edit to your workflow in Studio
creates a **new config version** (`cfg_...` with a version number). Pin a
`config_id` when you run jobs so you always execute the exact version you tested
(see `agent-api.md`). When you build a new config via the API, set it as the
Agent's default once you're happy with it so UI runs use it too.

---

## The step envelope (what a config is made of)

A config is a list of **steps**. Every step, whatever its type, has the same
envelope; only `data` differs by type:

```json
{
  "name": "extract_invoice",
  "type": "information-extract",
  "data": { "...type-specific settings..." },
  "is_first": false,
  "next_steps": [ { "step_name": "check_totals" } ]
}
```

| Field | Notes |
|-------|-------|
| `name` | Unique within the config. `next_steps` and validate checks refer to steps by this name. For an **extract** step, `name` must equal the schema's `text.format.name` or downstream wiring fails to resolve. |
| `type` | One of six: `document-parse`, `document-classify`, `information-extract`, `instruct`, `merge`, `validate`. |
| `data` | Type-specific settings (sections below). **Unknown keys inside `data` are accepted and stored silently at config creation and only fail when a job runs** — check every key against this guide. |
| `is_first` | Exactly one step carries `true`; it must be the parse step. |
| `next_steps` | List of `{ "step_name": ..., "condition": {...}? }`. An entry without a `condition` is the fallback. `[]` ends the workflow. |

Order rules: parse first; classify (optional) before extract; instruct anywhere
after parse; `merge` joins several branches into one; `validate` follows an
extract, instruct, or merge. A full config example is in `agent-api.md`.

---

## Parse step settings

The parse step (`document-parse`) turns the document into text/HTML that every
later step reads. Its `data`:

| Key | Default | Notes |
|-----|---------|-------|
| `model` | `document-parse` | The unpinned alias resolves to the current build; pin a dated build only for a reason. |
| `mode` | `standard` | `standard` or `enhanced`. |
| `ocr` | `force` | `force` (always OCR) or `auto` (only when the file has no text layer). |
| `output_formats` | `["html", "text"]` | Any of `html`, `text`, `markdown`, `pdf`. **Keep `html` whenever an extract follows** — the extract step reads the HTML; `["text"]` alone fails the job at run time. |
| `coordinates` | `true` | Include element coordinates in the parse output. |
| `chart_recognition` | `true` | Recognise charts. |
| `merge_multipage_tables` | `false` | Join tables that span pages. |

```json
{ "name": "parse", "type": "document-parse",
  "data": { "ocr": "auto", "output_formats": ["html", "text"], "coordinates": true },
  "is_first": true, "next_steps": [ { "step_name": "classify" } ] }
```

---

## Extraction schema

The extraction step uses a **JSON Schema** describing the fields you want back.
It lives inside the step's `text.format`:

```json
{
  "type": "json_schema",
  "name": "submission_fields",
  "schema": {
    "type": "object",
    "properties": {
      "applicant_name": {
        "type": "string",
        "description": "The legal name of the applicant business, exactly as written."
      },
      "years_in_business": {
        "type": "integer",
        "description": "Number of years the business has operated, only if given as a count of years."
      },
      "total_receipts": {
        "type": "number",
        "description": "Annual gross sales / total receipts in dollars, digits only."
      },
      "lines_of_business_requested": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "line_of_business": { "type": "string" }
          }
        },
        "description": "Each coverage line requested on the application."
      }
    },
    "required": ["applicant_name"]
  }
}
```

**Rules that keep a schema valid:**

- Top-level fields may be `string`, `number`, `integer`, `boolean`, or `array`.
  To group repeating rows (a loss-history table, a list of coverages), use an
  **array of objects**, as shown above.
- Nesting goes up to three levels: root → array → object → primitive. There is
  no fourth level — a nested object inside a row object won't validate, so
  flatten it into columns on the row.
- Field names shouldn't start with `_`.
- `mode` in the **extract** step's `data` selects `standard`, `enhanced`
  (vision-enhanced; enhanced supports up to 50 pages, standard up to 1,000),
  or `auto` (the service picks per document).
- `confidence` (default `true`) adds a `high` / `medium` / `low` confidence
  label per field to the extract output; `location` (default `true`) adds the
  source location of each value. Both are step settings, not schema keys.
- **The envelope matters.** The extraction step takes the `json_schema` object
  shown above — `{"type": "json_schema", "name": ..., "schema": {...}}` — on
  its own. Only the **classify** step wraps its schema in a `response_format`
  object. Wrapping an extraction schema in `response_format` (or unwrapping a
  classify one) is a common copy-paste error and reads as an invalid schema.

**Size limit: the schema string is hard-capped.** Creating a config is rejected
outright if the extraction step's schema serializes to more than 15,000
characters — a flat request rejection at config-creation time, not a quality
warning, and you find it when a rewrite you liked won't save.

- **Measure it the way the API does:** `len(json.dumps(schema))` with Python's
  **default separators**. A compact/minified measurement
  (`separators=(",", ":")`) understates it by a few hundred characters, which is
  enough to read "safe" on a schema that gets rejected.
- **Budget ≤ 12,000** on that measurement. That leaves room to add an instruct
  step or a few more descriptions later without a compression pass over
  everything you already tuned. Treat the budget as a design input before you
  start writing descriptions, not a ceiling you discover.

**Keep examples in descriptions generic.** One or two examples in a field
description help; a string lifted verbatim from a document you are going to
score does not — it turns the schema into its own answer key, and the field
looks solved on that document and nowhere else. Write the shape
("`YYYY-MM-DD`", "a dollar figure, digits only"), not the answer.

**Descriptions do the heavy lifting.** The field name tells the model *what to
call* the value; the description tells it *what the value is and how to handle
it*. Precise descriptions produce clean, consistent output. For example:

- Distinguish similar answers: "Years in business — only if given as a count of
  years. If a four-digit start year is written instead, leave this blank and put
  it in `business_start_date`."
- Point at the right source: "Total receipts — the annual gross sales figure,
  not payroll."
- Constrain the format: "Digits only, no currency symbols or commas."

**When to stop editing descriptions.** Description edits have a floor. If you
have made about three scored changes to the same field and none of them moved
the number by more than the run-to-run spread you measured (see
`scoring-guide.md` — "Run it more than once"), a fourth wording is not a fourth
experiment. Close that field to prose edits and change a different variable:
**input framing** — split a multi-document packet so the field is extracted from
one document instead of a pile; **a ruling** — take the scope question to
whoever owns the process and write the answer into ground truth; or **parse
quality** — check what the model is actually reading before blaming what it was
asked. A field that has plateaued is a finding worth recording, not a failure to
keep grinding at.

**Start with required fields.** List the fields your downstream system must have
as `required`, get those accurate first, then add optional fields as a bonus.
Scoring your required fields on their own gives you the number that matters most.

**Updating the schema via API.** Because config steps are immutable once
created, you "update" a schema by creating a new config (clone the current one,
swap in the new schema, then set it default). There are also preset helper
agents — `schema-generate` and `schema-update` — that draft or revise a schema
for you when you call `POST /v2/responses` with that `model` and pass your
intent in `text`.

**Auto-generate.** The extraction step has an auto-generate option that inspects
a document and proposes a schema of the fields someone would typically pull from
it — a fast starting point you then refine.

---

## Class map (classification)

Classification tags each document (or each split section of a PDF) with one of
the types you define. It reads the document's **content**, not just its title,
so it recognizes many layouts of the same underlying form.

The class list lives in the classify step's `text.format` as a string field with
a `oneOf` list of categories:

```json
{
  "type": "json_schema",
  "name": "doc_type",
  "schema": {
    "type": "string",
    "oneOf": [
      { "const": "commercial_application", "description": "A commercial insurance application form." },
      { "const": "general_liability", "description": "A general liability coverage section." },
      { "const": "supplemental_application", "description": "A carrier or class-specific supplemental questionnaire." },
      { "const": "other", "description": "Anything that does not match the above." }
    ]
  }
}
```

**Routing to per-class schemas.** Add a `next_steps[]` list on the classify step
with a condition per class, so each type flows to its own extraction step:

```json
"next_steps": [
  { "step_name": "extract_supplemental",
    "condition": { "field": "text", "operator": "==", "value": "supplemental_application" } },
  { "step_name": "extract_default" }
]
```

The condition matches on `field: "text"` — the predicted class string. Include a
final branch with no condition as the fallback. Always include an `other`
category and a fallback branch so every document has a home.

**Splitting combined PDFs.** Set `"split": true` on the classify step and it will
break a multi-document PDF (e.g. an application + a supplemental + an email in
one file) into sections, classify each, and route each to its matching schema —
so you can start straight from a raw package. A working split step carries
**both** a `split_criteria` list and the `text.format` class schema:

```json
{ "name": "classify", "type": "document-classify",
  "data": {
    "split": true,
    "split_criteria": [
      { "criterion": "commercial_application", "description": "The main application form." },
      { "criterion": "supplemental_application", "description": "A supplemental questionnaire." }
    ],
    "text": { "format": { "type": "json_schema", "name": "doc_type",
              "schema": { "type": "string", "oneOf": [ "...as above..." ] } } }
  },
  "next_steps": [ "...one conditional branch per class, plus a fallback..." ] }
```

`split_criteria` (prose, one entry per unit you want split out) drives how pages
are **grouped**; `text.format` drives the **label** each group gets and is the
value `next_steps` routes on — it is required even when criteria are present.
Keep the two lists aligned (same names) so the grouping and the label agree.
Each split child then flows through the rest of the workflow on its own, and
comes back in the job output as its own result (see `agent-api.md`).

**Subclasses.** If one class arrives in two very different shapes (a clean form
vs. a free-text narrative, say), you can split it into two subclasses and give
each its own tuned schema. Classification works best when the categories have
clear, distinguishing content; the more distinct the classes, the more reliable
the routing.

---

## Instruct prompt

The instruct step runs a free-form prompt over the upstream results — useful for
a completeness check, an appetite/eligibility gate, a routing recommendation, or
a short summary that drives your own automation.

```json
{
  "input": [
    { "role": "user", "content": [
      { "type": "input_text",
        "text": "Given the extracted fields, state whether the submission is complete (all required fields present), whether it fits appetite, and a one-line routing recommendation with rationale." }
    ] }
  ]
}
```

The prompt lives in `data.input` in exactly that shape — a list with one
`user` message whose `content` is a list of `input_text` items. **Do not use a
`prompt` key**: it is accepted and stored when you create the config, and the
job then fails at run time with "queries are required for instruct".

It automatically receives the parse/classify/extract output as context, so you
don't wire the data in by hand. Instruct prompts work well as prose; you can add
a `text.format` if you want a structured result. You can edit the instruct
prompt by pasting a new prompt into the step, or add/replace instruct steps via
the API.

---

## Merge and validate

Two utility steps complete the six node types.

**Merge** (`type: "merge"`) joins several branches — typically the per-class
extract steps after a splitting classify — into one downstream context, so a
single instruct or validate can see all of them. It has no settings: `data` must
be `null` (anything else is rejected). Its own output is provenance only (which
branches arrived, their page ranges); the values stay on the extract steps.

```json
{ "name": "merge_all", "type": "merge", "data": null,
  "next_steps": [ { "step_name": "check_completeness" } ] }
```

**Validate** (`type: "validate"`) runs named checks against extract output and
emits a three-lane verdict: any failed `error` check → `red`; only `warning`
checks failed → `yellow`; all pass → `green`.

```json
{ "name": "check_completeness", "type": "validate",
  "data": {
    "checks": [
      { "name": "invoice number present", "severity": "error",
        "condition": { "left": { "node": "extract_invoice", "field": "invoice_number" },
                       "operator": "filled" } },
      { "name": "total is positive", "severity": "warning",
        "condition": { "left": { "node": "extract_invoice", "field": "total_amount" },
                       "operator": "gt", "right": { "const": 0 } } }
    ]
  },
  "next_steps": [
    { "step_name": "route_auto",   "condition": { "field": "text", "operator": "==", "value": "green" } },
    { "step_name": "route_review", "condition": { "field": "text", "operator": "==", "value": "yellow" } },
    { "step_name": "route_reject", "condition": { "field": "text", "operator": "==", "value": "red" } }
  ] }
```

- `severity` is `"error"` or `"warning"`, exactly.
- Nine operators: `filled`, `eq`, `neq`, `gt`, `lt`, `gte`, `lte`, `contains`,
  `matches` (anchored regex). `filled` takes no `right`; an empty string fails it.
  `gt`/`lt`/`gte`/`lte` compare as numbers and fail on non-numeric values.
- An operand is `{ "node": "<extract step name>", "field": "<top-level field>" }`
  or a literal `{ "const": ... }`. `field` is a flat top-level lookup — there is
  no path syntax into arrays or nested rows.
- Checks can be grouped: `{ "logic": "and" | "or", "conditions": [ ... ] }`.
- Downstream routing branches on `field: "text"` with the lane string — the same
  mechanism as classify routing.
- Validate evaluates the full check list once per split child. A check that
  refers to an extract step that did not run on that child's branch resolves to
  `null` and fails, so after a splitting classify put a `merge` in front of the
  validate (then each operand is read across all the branches that arrived), or
  put one validate on each branch.

---

## Building with an AI coding agent

Schema iteration is a natural fit for a coding agent (Claude, Codex, etc.). Give
it this guide, an example document, and the shape of your target output, and ask
it to draft field descriptions and generate a few schema variants. Run each
variant with `scripts/run_agent.py`, score them with `scripts/score.py`, and
keep the best. Describing the *intent* behind each field ("this populates the
applicant name on our application") tends to produce the strongest descriptions.
