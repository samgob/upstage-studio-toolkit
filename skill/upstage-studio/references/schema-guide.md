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
- Nesting goes up to three levels: root → array → object → primitive.
- Field names shouldn't start with `_`.
- `mode` on the step selects the model tier: `standard`, `auto`, or `enhanced`
  (vision-enhanced; enhanced supports up to 50 pages, standard up to 1,000).

**Descriptions do the heavy lifting.** The field name tells the model *what to
call* the value; the description tells it *what the value is and how to handle
it*. Precise descriptions produce clean, consistent output. For example:

- Distinguish similar answers: "Years in business — only if given as a count of
  years. If a four-digit start year is written instead, leave this blank and put
  it in `business_start_date`."
- Point at the right source: "Total receipts — the annual gross sales figure,
  not payroll."
- Constrain the format: "Digits only, no currency symbols or commas."

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
so you can start straight from a raw package.

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

It automatically receives the parse/classify/extract output as context, so you
don't wire the data in by hand. Instruct prompts work well as prose; you can add
a `text.format` if you want a structured result. You can edit the instruct
prompt by pasting a new prompt into the step, or add/replace instruct steps via
the API.

---

## Building with an AI coding agent

Schema iteration is a natural fit for a coding agent (Claude, Codex, etc.). Give
it this guide, an example document, and the shape of your target output, and ask
it to draft field descriptions and generate a few schema variants. Run each
variant with `scripts/run_agent.py`, score them with `scripts/score.py`, and
keep the best. Describing the *intent* behind each field ("this populates the
applicant name on our application") tends to produce the strongest descriptions.
