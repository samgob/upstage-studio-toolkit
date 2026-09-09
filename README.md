# Upstage Studio Toolkit

Tools and references for working with an [Upstage AI Studio](https://studio.upstage.ai)
document-processing **Agent** over the API: run documents through it (one at a
time or by the folder), read every step's output, edit and version the workflow
as JSON, and — for proof-of-concept evaluation — score extraction accuracy
against your own ground truth. Everything is Python 3.8+ standard library:
nothing to install.

**Version 2.6.0** — see [CHANGELOG.md](CHANGELOG.md).

## Who this is for

| You are… | Start with |
|----------|-----------|
| **An integrator** wiring a Studio Agent into your own application (any language, any platform) | [`INTEGRATION_QUICKSTART.md`](INTEGRATION_QUICKSTART.md) — the HTTP flow with curl: upload → create job with your correlation ID → poll (no webhooks) → read every step's output → retention, caching, errors, concurrency. Then `skill/upstage-studio/references/agent-api.md` for the full endpoint and per-step reference. |
| **Evaluating an Agent on your documents** (a POC) | `skill/upstage-studio/SKILL.md` — run a folder, read the results, edit the schema, score accuracy. |
| **Non-technical** — you want to run a folder of documents and look at the results | `gui/` — a local web GUI over the batch tool (`python3 gui/upstage_batch_gui.py`). |
| **Using Claude Code or another coding agent** | Install `skill/upstage-studio` as a skill (below) and ask it to run, integrate, or iterate on your Agent. |

## Install

```bash
git clone https://github.com/samgob/upstage-studio-toolkit.git
cd upstage-studio-toolkit
export UPSTAGE_API_KEY="<key created in the Upstage Console>"
```

That's a complete install — every script in the clone runs as-is.

**Claude Code users:** make the skill available by copying or symlinking it
into your skills directory:

```bash
ln -s "$(pwd)/skill/upstage-studio" ~/.claude/skills/upstage-studio    # or cp -r
```

**Everyone else:** run the scripts directly.

## Quick start

```bash
# One document through your Agent — every step's output, as one JSON file
python3 skill/upstage-studio/scripts/run_agent.py \
    --agent agt_XXX --config-id cfg_XXX --input invoice.pdf --output results/

# A folder, at scale (parallel workers, retry/backoff, checkpoint + --resume)
python3 skill/upstage-studio/scripts/upstage_batch.py agent \
    --agent-id agt_XXX --config-id cfg_XXX --docs ./invoices/ --workers 5

# Score extraction results against your ground truth (POC evaluation)
python3 skill/upstage-studio/scripts/score.py \
    --config skill/upstage-studio/examples/scoring_config.example.json
```

The per-document JSON both runners write is generic: every step that ran, in
API order, named as in your config, with split children kept as a list per step
name — parse, classify, extract, instruct, merge, and validate alike.

## Layout

```
INTEGRATION_QUICKSTART.md          ← HTTP-first integration guide (start here if you're integrating)
skill/upstage-studio/
  SKILL.md                         ← walkthrough (also the Claude skill entry point)
  scripts/run_agent.py             ← minimal upload → run → poll → JSON, single file or folder
  scripts/upstage_batch.py         ← the batch CLI (canonical copy; also stamped into gui/ by build.sh)
  scripts/score.py                 ← accuracy scorer
  references/agent-api.md          ← endpoint + per-step output reference, worked example
  references/schema-guide.md       ← the step envelope; parse, schema, classes, instruct, merge, validate
  references/scoring-guide.md      ← ground-truth format and metric
  examples/                        ← copy-paste schema / ground-truth / scoring-config templates
gui/
  upstage_batch_gui.py             ← local web GUI over the batch CLI
  README.md
build.sh                           ← stamps the batch CLI into gui/ and packages dist/*.zip
CHANGELOG.md
```

## Building the zips (maintainers)

```bash
./build.sh
# → dist/upstage-studio.zip       (the skill + scripts)
# → dist/upstage-batch-gui.zip    (batch CLI + web GUI)
```

`skill/upstage-studio/scripts/upstage_batch.py` is the single source for the
batch CLI; `gui/upstage_batch.py` is a build-time copy and is git-ignored. Edit
the skill copy.

## Security and privacy

- No secrets or customer data live in this repo, and `.gitignore` blocks logs,
  run outputs, `results/`, and anything key-shaped. Keep API keys and real
  document outputs out of the tree.
- API keys are read from `UPSTAGE_API_KEY` (or `--key`), never hard-coded.
- The GUI serves loopback requests only (it validates the `Host` header),
  fetches no third-party assets, and passes the API key to the batch process
  through the environment rather than the command line.

## Support

Open an issue on this repository, or contact your Upstage representative.

## License

MIT — see [LICENSE](LICENSE).
