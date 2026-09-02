# Upstage Studio Toolkit

Customer-facing tooling for working with an [Upstage AI Studio](https://studio.upstage.ai)
document-processing Agent — running documents at scale, editing and versioning
your schema, and scoring extraction accuracy. Everything here is self-contained
Python 3.8+ (standard library only) and ships in two forms for two audiences.

## Two delivery paths

| Path | For | What's in it |
|------|-----|--------------|
| **`skill/upstage-studio/`** | Technical / AI-native users (drive it with Claude, Codex, etc.) | The Claude skill: API reference, schema-editing and scoring guides, a minimal `run_agent.py` example, the `score.py` accuracy scorer, worked examples, **and** the batch CLI as its at-scale engine. |
| **`gui/`** | Non-technical users who'd rather click than code | The batch CLI plus a local web GUI (`upstage_batch_gui.py`) — browse to a folder, click Run, watch progress, browse results. |

Both are packaged as standalone zips by `build.sh` (see below).

## Single source of truth (no fork/fracture)

The batch CLI, `core/upstage_batch.py`, is the **one canonical copy**. Both
delivery paths use it, but neither keeps its own edited version — `build.sh`
stamps the canonical file into each artifact at build time. So:

- **Edit only `core/upstage_batch.py`.** The copies in `skill/.../scripts/` and
  `gui/` are build outputs (git-ignored) and get overwritten on every build.
- Each delivered zip is still fully self-contained — customers never need to
  assemble anything.

```
core/upstage_batch.py          ← edit here (the only source)
skill/upstage-studio/          ← the skill (run_agent.py + score.py are skill-owned)
gui/upstage_batch_gui.py       ← the web GUI
build.sh                       ← assembles dist/upstage-studio.zip + dist/upstage-batch-gui.zip
```

## Build & deliver

```bash
./build.sh
# → dist/upstage-studio.zip       (send to technical users)
# → dist/upstage-batch-gui.zip    (send to non-technical users)
```

## Security & privacy posture

- **No secrets or customer data in this repo.** `.gitignore` blocks logs, run
  outputs, `results/`, `*.rtf`, and anything key-shaped. Keep API keys and real
  document outputs out of the tree entirely.
- The GUI serves only loopback requests (validates the `Host` header), fetches
  no third-party assets, and passes the API key to the batch process via the
  environment rather than the command line.
- API keys are read from `UPSTAGE_API_KEY` (or `--key`), never hard-coded.

## Changes 2026-09

- `score.py` gains `--extractions-dir` and `--run-label`, so you can score
  several runs of one config without cloning the config file, and every report
  now opens with a `SCOPE:` line (documents, cells, TN policy, run label).
- `tn_policy` is now required in the scoring config — the scorer names the two
  choices instead of silently defaulting one. Scoring math is unchanged.
- New guidance on measuring honestly: run each config n ≥ 3 times and report the
  spread, score on the model you'll actually deploy, and get the answer key
  before writing ground truth (`references/scoring-guide.md`).
- Schema guide adds the hard schema-size limit and how to measure it, the
  generic-examples rule, the `json_schema` vs `response_format` envelope, and
  when to stop editing field descriptions; parse and config-hygiene notes added
  to `SKILL.md` / `references/agent-api.md`.

## License

MIT — see [LICENSE](LICENSE).
