# upstage-studio

A small, self-contained toolkit for working with an **Upstage AI Studio Agent**
(a document-processing workflow) from the command line — run documents through
the API and get every step's output, edit and version your workflow as JSON,
and (for POC evaluation) score accuracy against your own ground truth.

It's packaged as a **Claude skill**: copy or symlink the `upstage-studio/`
folder into `~/.claude/skills/` and your coding agent can drive it directly. The
scripts also run standalone with plain Python 3.8+ (standard library only —
nothing to install).

Integrating from your own application? Read `INTEGRATION_QUICKSTART.md` at the
repo root first.

## Quick start

```bash
export UPSTAGE_API_KEY="<key created in the Upstage Console>"

# Run one document through your Agent; every step's output is captured
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX --input document.pdf

# Batch a folder, saving one JSON result per document
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX \
    --input ./docs/ --workers 5 --output results/

# Score results against your ground truth
python scripts/score.py --config examples/scoring_config.example.json
```

## Contents

| Path | What it is |
|------|-----------|
| `SKILL.md` | Skill entry point + full walkthrough |
| `scripts/run_agent.py` | Minimal example — upload → run → poll → one JSON per document (single file or folder) |
| `scripts/upstage_batch.py` | Batch-at-scale engine (parallel, retry, resume, PDF repair, V1 modes) |
| `scripts/score.py` | Accuracy scoring vs. ground truth |
| `references/agent-api.md` | Agent API reference: endpoints, per-step output shapes, worked example |
| `references/schema-guide.md` | The step envelope; parse settings, schema, classes, instruct, merge, validate |
| `references/scoring-guide.md` | Ground-truth format + scoring method |
| `examples/` | Copy-paste schema, ground-truth, and config templates |

## What you'll need

- An **API key**, created in the Upstage Console and sent as
  `Authorization: Bearer <key>` (the scripts read `UPSTAGE_API_KEY`).
- Your **Agent ID** (`agt_...`) and the **Config ID** (`cfg_...`) / version you
  want to run, from Studio.
