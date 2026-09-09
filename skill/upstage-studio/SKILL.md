---
name: upstage-studio
description: Run, integrate, test, and score an Upstage AI Studio document-processing Agent from the command line or your own code. Use this skill to run documents through a Studio Agent workflow via the API (single files or whole folders), read every step's output (parse, classify, extract, instruct, merge, validate), edit and version the extraction schema / classes / instruct prompt, and measure extraction accuracy against your own ground truth. Trigger on "run this through the agent", "integrate the Studio API", "call the agent from our app", "test the workflow", "batch process these docs", "update the schema", "iterate the schema", "score the extraction", "check accuracy", "Studio API", "Upstage agent", or any request to exercise, integrate, or improve an Upstage Studio pipeline.
---

# Upstage AI Studio — Agent Toolkit

A self-contained toolkit for working with an Upstage AI **Studio Agent** (a document-processing workflow) programmatically. Everything the Studio UI does — parse, classify, extract, instruct, merge, validate — is also available through the API, so you can wire the workflow into your own systems, automate testing, and iterate on your schema quickly.

Three things this skill helps you do:

1. **Run documents** through your Agent via the API — one file or a whole folder at a time — and get every step's structured result back.
2. **Edit and version** your extraction schema, class map, instruct prompt, and validate checks as plain JSON.
3. **Score** extraction results against your own ground truth to measure accuracy field by field (for proof-of-concept evaluation).

**Integrating from your own application (any language)?** Start with `INTEGRATION_QUICKSTART.md` in the toolkit repository (at the repo root, alongside this skill's folder) — the HTTP-level flow with curl, per-step output shapes, retention, caching, and error handling. The scripts here are a reference implementation of the same calls.

Toolkit version: **2.6.0** (see `CHANGELOG.md` at the repo root).

## Setup (one time)

1. Sign in at **[console.upstage.ai](https://console.upstage.ai)** and create an **API key**. Every request sends it as `Authorization: Bearer <key>`.
2. Export it so the scripts can read it:
   ```bash
   export UPSTAGE_API_KEY="<your key>"
   ```
   (Add that line to your shell profile, or pass `--key <your key>` to any script.)
3. Note two IDs from your Agent in Studio ([studio.upstage.ai](https://studio.upstage.ai)):
   - **Agent ID** — looks like `agt_...` (identifies the workflow).
   - **Config ID** — looks like `cfg_...`, with a matching version number (`1`, `2`, `3`, …). Every edit you make in Studio creates a new config version, so pinning a config ID means you always run the exact version you tested.

That's it — the scripts use only the Python 3.8+ standard library, so there's nothing to `pip install`.

## Quick start

```bash
# Run one document through your Agent; every step's output is captured by default
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX --input path/to/document.pdf

# Batch a whole folder at scale (parallel, retry/backoff, checkpoint + resume)
python scripts/upstage_batch.py agent --agent-id agt_XXX --config-id cfg_XXX \
    --docs path/to/folder/ --workers 5

# (run_agent.py also handles a folder — it's the minimal version to read and adapt)

# Score a folder of extraction results against your ground truth
python scripts/score.py --config examples/scoring_config.example.json
```

## The workflow: how a job runs

An Agent job is three API calls. `scripts/run_agent.py` does all three for you; the sequence is worth knowing when you integrate it into your own code:

1. **Upload the file** → `POST /v2/files` returns a `file_id`. The file is ready for jobs once its status is `UPLOADED` (page-image conversion runs first — the script waits for you).
2. **Create the job** → `POST /v2/responses` with your `model` (the Agent ID), optional `config_id` (the version pin), the `file_id`, and `include: ["all"]` to get every step's output. A `metadata` object is accepted here but, on the current API, is **not returned** on reads — only server-set keys such as `source` come back — so keep your own job-ID → document map (the scripts key results by filename).
3. **Poll for the results** → `GET /v2/responses/{job_id}?include[]=all` until `status` is `completed` or `failed`. **Polling is the only completion signal — there are no webhooks.**

**Reading the output.** The response's `output[]` has one item per step that ran, in execution order. The step's **name** is the item's `model` key; its results are in `content[]` — one entry normally, several when the step ran once per split child. `content[].text` holds the step's output (a JSON string for classify, extract, validate, and merge; parse it), and `content[].additional_values` is a JSON string of per-step extras (classify confidence and page ranges, validate check trace, merge provenance). `run_agent.py` writes this as one JSON per document: `steps[]` in API order plus a `by_step` map of name → list of results. See `references/agent-api.md` for the per-step shapes and a worked multi-step example.

**Getting every step's output.** By default a job returns only the *last* step. To get all of them, pass `include: ["all"]` when creating the job, and use the **bracketed array form** `?include[]=all` when reading it back — it has worked consistently, while the unbracketed form has been observed to be ignored (you then get only the last step). `run_agent.py` defaults to `all`; pass `--include last` for the final step only. A `failed` job still returns every step that completed, with `error.step` naming the failing step; the scripts write that partial output rather than discarding it.

**Version pinning.** Pass `config_id` (or its version number) on every job so you always run the exact workflow version you validated. Omit it and the Agent runs its current default.

**Config hygiene.** Testing variations creates configs quickly. Give experiments a name prefix (`test_`, `diag_`) so they're identifiable, delete them once the decision is made, and — after any batch of config creates — confirm which config actually carries `is_default` before anyone runs the Agent from the Studio UI. API jobs pin their version; UI runs take the default, so the two can quietly diverge. See `references/agent-api.md`.

See `references/agent-api.md` for the full endpoint reference (auth, file input methods, job listing, pagination, error codes, retention, and caching).

## Editing the workflow: schema, classes, instruct prompt, checks

Your Agent's behaviour is a **config**: a list of steps, each `{name, type, data, is_first, next_steps}`. Six step types exist — `document-parse`, `document-classify`, `information-extract`, `instruct`, `merge`, `validate` — and everything you set in the Studio UI is plain JSON in a step's `data` that you can export, edit, and load back. `references/schema-guide.md` covers the envelope, each step's settings, and the handful of rules that keep a config valid, with copy-paste examples in `examples/schema.example.json`.

The short version:

- **Extraction schema** — a JSON Schema of the fields you want. Start with your *required* fields (the ones your downstream system must have), get those accurate, then layer optional fields on top. Field *descriptions* are where you tell the model exactly what a field means and how to handle the answer — the clearer the description, the cleaner the output.
- **Class map** — a list of the document types your Agent should recognize, each with a plain-English description. Classification looks at document *content*, not just the title, so it reliably groups many layouts of the same underlying form. With `split: true` plus `split_criteria` it also splits a multi-document PDF and routes each section to its own schema.
- **Instruct prompt** — a free-form prompt (in `data.input`) that runs on the extracted data, e.g. a completeness or appetite check whose result drives your own automation.
- **Merge + validate** — `merge` joins the per-class branches back into one; `validate` runs named checks (`filled`, `eq`, `gt`, `matches`, …) and emits a `green` / `yellow` / `red` lane you can route on.

Parse step settings (`mode`, `ocr`, `output_formats`, `coordinates`) are in `references/schema-guide.md` → "Parse step settings".

**Tip: let an AI agent do the schema grind.** Point Claude (or your coding agent of choice) at `references/schema-guide.md` and your target output, and have it draft and A/B-test schema variations for you. Describe the *intent* of each field — "this fills the applicant name on our application" — and the model can generate strong field descriptions, then you score the results (below) to pick the winner.

## Model options

Your Agent isn't locked to one model. You can pick a different model per step (one for classify, another for extract), across a range that runs from Upstage's own lightweight Solar models — including a vision model, and the option to run them inside your own VPC or on-prem — through larger standard and vision-enhanced models. The Studio UI's model picker is the live list for your account; ask your Upstage representative to enable others.

**Document Parse.** The unpinned `document-parse` alias resolves to the current build, which is usually what you want; pin a build only when you have a reason to, and re-check that reason when you revisit the pipeline.

- **Checkboxes:** builds have differed in how they render a *filled* checkbox. If your documents have checkboxes, parse a sample page on the build you intend to pin and confirm the selected box comes through as a distinct glyph — a build that drops it fails silently and looks like a schema problem downstream.
- **Long documents:** before you score anything, spot-check a few sentences that cross a page break in the parse output. Continuations across a break are where layout reconstruction is most likely to reorder a word or mistype a fragment as a heading, and a parse problem found on day three invalidates days one and two.

---

## For POC evaluation: scoring accuracy and schema engineering

Integrators wiring the API into an application can skip this section. It is for teams evaluating an Agent on their own documents before committing.

### Scoring accuracy

`scripts/score.py` measures how well the extraction matches ground truth *you* define. It reports two numbers for every field:

- **Raw** — exact string match.
- **Normalized** — after applying your own normalization rules (e.g. trim whitespace, standardize dates) to both the ground truth and the output, then comparing again.

You always see both, so you know how much of the accuracy comes from formatting vs. genuine content. The headline number is a **micro-accuracy** (per-field true-positives over all scored slots), plus per-field and per-document breakdowns and an error summary (missed, extra, and mismatched values).

Ground truth is one small JSON file per document. `references/scoring-guide.md` explains the format and the metric — a transparent micro-accuracy in the spirit of key-information-extraction evaluation (see Khang et al., *KIEval: Evaluation Metric for Document Key Information Extraction*, arXiv:2503.05488); `examples/ground_truth.example.json` and `examples/scoring_config.example.json` are ready to copy. The scorer reads the per-document JSON that `run_agent.py --output` or the batch CLI writes and finds the extraction step on its own.

**A good testing loop:** score your *required* fields first → read the per-field misses → sharpen those field descriptions or normalization rules → re-run → re-score. A few iterations usually gets required-field accuracy where you want it.

**Run each config more than once.** Extraction isn't deterministic, so one run is a draw, not a measurement. Score the same config three times (five for a number you'll quote), report mean and spread, and only treat a change as an improvement when it beats that spread. `score.py --extractions-dir <run> --run-label <name>` scores repeat runs of one config without cloning the config file, and every report opens with a `SCOPE:` line naming what the number covers. Details in `references/scoring-guide.md`.

**Score on the model you will deploy.** If production will run in your VPC, the number that matters comes from the model that can run there. A larger model is worth running as a comparison to see the headroom, but it shouldn't be the one you tuned and quoted.

### Schema engineering

The schema-size limit, the generic-examples rule, when to stop editing a field description, and the `json_schema` vs `response_format` envelope are all in `references/schema-guide.md` → "Extraction schema".

---

## Files in this skill

| Path | What it is |
|------|-----------|
| `scripts/run_agent.py` | Minimal, readable example — upload → run → poll → write one JSON per document with every step's output. Start here to see the flow; adapt it into your own code. |
| `scripts/upstage_batch.py` | The batch tool to actually run at scale — parallel workers, retry/backoff, checkpoint + `--resume`, PDF auto-repair, and V1 fallback modes. Same three-call flow under the hood; same generic per-step output. |
| `scripts/score.py` | Score extraction results against your ground truth (POC evaluation). |
| `references/agent-api.md` | Full Agent API reference (endpoints, params, per-step output shapes, worked example, errors, retention, caching). |
| `references/schema-guide.md` | The step envelope and how to edit parse settings, schema, classes, instruct prompt, merge, and validate checks. |
| `references/scoring-guide.md` | Ground-truth format and how scoring works. |
| `examples/` | Copy-paste schema, ground-truth, and scoring-config templates. |

## Support

If a job errors or a document behaves unexpectedly, the API returns a specific error code and message (see `references/agent-api.md`). For anything else, open an issue on this repository or contact your Upstage representative.
