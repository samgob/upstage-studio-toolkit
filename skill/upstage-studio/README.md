# upstage-studio

A small, self-contained toolkit for working with an **Upstage AI Studio Agent**
(a document-processing workflow) from the command line — run documents through
the API, edit and version your extraction schema, and score accuracy against
your own ground truth.

It's packaged as a **Claude skill**: drop the `upstage-studio/` folder into
`.claude/skills/` and your coding agent can drive it directly. The scripts also
run standalone with plain Python 3.8+ (standard library only — nothing to
install).

## Quick start

```bash
export UPSTAGE_API_KEY="up_..."          # from studio.upstage.ai

# Run one document through your Agent, printing every step
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX \
    --input document.pdf --include all

# Batch a folder, saving one JSON result per document
python scripts/run_agent.py --agent agt_XXX --config-id cfg_XXX \
    --input ./docs/ --include all --workers 5 --output results/

# Score results against your ground truth
python scripts/score.py --config examples/scoring_config.example.json
```

## Contents

| Path | What it is |
|------|-----------|
| `SKILL.md` | Skill entry point + full walkthrough |
| `scripts/run_agent.py` | Minimal example — upload → run → retrieve (single file or folder) |
| `scripts/upstage_batch.py` | Batch-at-scale engine (parallel, retry, resume, PDF repair, V1 modes) |
| `scripts/score.py` | Accuracy scoring vs. ground truth |
| `references/agent-api.md` | Agent API reference |
| `references/schema-guide.md` | Editing the schema, classes, instruct prompt |
| `references/scoring-guide.md` | Ground-truth format + scoring method |
| `examples/` | Copy-paste schema, ground-truth, and config templates |

## What you'll need from Studio

- An **API key** (`up_...`) from the console.
- Your **Agent ID** (`agt_...`) and the **Config ID** (`cfg_...`) / version you
  want to run.
