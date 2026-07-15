---
name: upstage-studio
description: Test, iterate, and score an Upstage AI Studio document-processing Agent from the command line. Use this skill to run documents through a Studio Agent workflow via the API (single files or whole folders), edit and version the extraction schema / classes / instruct prompt, and measure extraction accuracy against your own ground truth. Trigger on "run this through the agent", "test the workflow", "batch process these docs", "update the schema", "iterate the schema", "score the extraction", "check accuracy", "Studio API", "Upstage agent", or any request to exercise or improve an Upstage Studio pipeline.
---

# Upstage AI Studio — Agent Toolkit

A self-contained toolkit for working with an Upstage AI **Studio Agent** (a document-processing workflow) programmatically. Everything the Studio UI does — parse, classify, extract, instruct — is also available through the API, so you can automate testing, wire the workflow into your own systems, and iterate on your schema quickly.

Three things this skill helps you do:

1. **Run documents** through your Agent via the API — one file or a whole folder at a time — and get structured results back.
2. **Edit and version** your extraction schema, class map, and instruct prompt as plain JSON.
3. **Score** extraction results against your own ground truth to measure accuracy field by field.

## Setup (one time)

1. Create a free account at **[studio.upstage.ai](https://studio.upstage.ai)** and grab an **API key** from the console (keys start with `up_`).
2. Export it so the scripts can read it:
   ```bash
   export UPSTAGE_API_KEY="up_..."
   ```
   (Add that line to your shell profile, or pass `--key up_...` to any script.)
3. Note two IDs from your Agent in Studio:
   - **Agent ID** — looks like `agt_...` (identifies the workflow).
   - **Config ID** — looks like `cfg_...`, with a matching version number (`1`, `2`, `3`, …). Every edit you make in Studio creates a new config version, so pinning a config ID means you always run the exact version you tested.

That's it — the scripts use only the Python 3.8+ standard library, so there's nothing to `pip install`.

## Quick start

```bash
# Run one document through your Agent and print every step's output
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX \
    --input path/to/document.pdf --include all

# Batch a whole folder at scale (parallel, retry/backoff, checkpoint + resume)
python scripts/upstage_batch.py agent --agent-id agt_XXX --config-id cfg_XXX \
    --docs path/to/folder/ --workers 5

# (run_agent.py also handles a folder — it's the minimal version to read and adapt)

# Score a folder of extraction results against your ground truth
python scripts/score.py --config examples/scoring_config.example.json
```

## The workflow: how a job runs

An Agent job is three API calls. `scripts/run_agent.py` does all three for you; the sequence is worth knowing when you integrate it into your own code:

1. **Upload the file** → `POST /v2/files` returns a `file_id`. The file is ready for jobs once its status is `UPLOADED` (image conversion runs first — the script waits for you).
2. **Create the job** → `POST /v2/responses` with your `model` (the Agent ID), optional `config_id` (the version pin), the `file_id`, and `include: ["all"]` to get every step's output.
3. **Get the results** → `GET /v2/responses/{job_id}?include[]=all` returns the output of each step (parse → classify → extract → instruct).

**Getting every step's output.** By default a job returns only the *last* step. To get all of them, pass `include: ["all"]` when creating the job, and use the **bracketed array form** `?include[]=all` when reading it back. `run_agent.py --include all` handles both for you. This is how you can, for example, store the classification result as file metadata, push the extraction to a database, and route on the instruct summary — all from one job.

**Version pinning.** Pass `config_id` (or its version number) on every job so you always run the exact workflow version you validated. Omit it and the Agent runs its current default.

See `references/agent-api.md` for the full endpoint reference (auth, file input methods, job listing, pagination, error codes, and caching).

## Editing the schema, classes, and instruct prompt

Your extraction schema, class map, and instruct prompt are all plain JSON that you can export from Studio, edit in your editor, and load back in. `references/schema-guide.md` covers the structure and the handful of rules that keep a schema valid, with copy-paste examples in `examples/schema.example.json`.

The short version:

- **Extraction schema** — a JSON Schema of the fields you want. Start with your *required* fields (the ones your downstream system must have), get those accurate, then layer optional fields on top. Field *descriptions* are where you tell the model exactly what a field means and how to handle the answer — the clearer the description, the cleaner the output.
- **Class map** — a list of the document types your Agent should recognize, each with a plain-English description. Classification looks at document *content*, not just the title, so it reliably groups many layouts of the same underlying form. It can also split a multi-document PDF and route each section to its own schema.
- **Instruct prompt** — a free-form prompt that runs on the extracted data, e.g. a completeness or appetite check whose result drives your own automation.

**Tip: let an AI agent do the schema grind.** Point Claude (or your coding agent of choice) at `references/schema-guide.md` and your target output, and have it draft and A/B-test schema variations for you. Describe the *intent* of each field — "this fills the applicant name on our application" — and the model can generate strong field descriptions, then you score the results (below) to pick the winner.

## Scoring accuracy

`scripts/score.py` measures how well the extraction matches ground truth *you* define. It reports two numbers for every field:

- **Raw** — exact string match.
- **Normalized** — after applying your own normalization rules (e.g. trim whitespace, standardize dates) to both the ground truth and the output, then comparing again.

You always see both, so you know how much of the accuracy comes from formatting vs. genuine content. The headline number is a **micro-accuracy** (per-field true-positives over all scored slots), plus per-field and per-document breakdowns and an error summary (missed, extra, and mismatched values).

Ground truth is one small JSON file per document. `references/scoring-guide.md` explains the format and the metric — a transparent micro-accuracy in the spirit of key-information-extraction evaluation (see Khang et al., *KIEval: Evaluation Metric for Document Key Information Extraction*, arXiv:2503.05488); `examples/ground_truth.example.json` and `examples/scoring_config.example.json` are ready to copy.

**A good testing loop:** score your *required* fields first → read the per-field misses → sharpen those field descriptions or normalization rules → re-run → re-score. A few iterations usually gets required-field accuracy where you want it.

## Model options

Your Agent isn't locked to one model. Studio offers a range — lightweight in-house models (great for cost-sensitive or in-VPC deployments) through larger standard and vision-enhanced models — and you can pick a different model per step (e.g. one for classify, another for extract). If you'd like additional models enabled for your account, ask your Upstage contact.

## Files in this skill

| Path | What it is |
|------|-----------|
| `scripts/run_agent.py` | Minimal, readable example — upload → run → retrieve, for a single file or a folder. Start here to see the flow; adapt it into your own code. |
| `scripts/upstage_batch.py` | The batch tool to actually run at scale — parallel workers, retry/backoff, checkpoint + `--resume`, PDF auto-repair, and V1 fallback modes. Same three-call flow under the hood. |
| `scripts/score.py` | Score extraction results against your ground truth. |
| `references/agent-api.md` | Full Agent API reference (endpoints, params, errors, caching). |
| `references/schema-guide.md` | How to structure and edit the schema, classes, and instruct prompt. |
| `references/scoring-guide.md` | Ground-truth format and how scoring works. |
| `examples/` | Copy-paste schema, ground-truth, and scoring-config templates. |

## Support

If a job errors or a document behaves unexpectedly, the API returns a specific error code and message (see `references/agent-api.md`), and your Upstage contact is happy to help you dig in.
