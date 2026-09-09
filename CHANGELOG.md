# Changelog

## 2.6.0 — 2026-09-09

Clone-and-go for integrators; generic per-step output.

**Output model (breaking for anyone parsing the old files)**

- `run_agent.py` now writes one JSON per document with every step the Agent
  ran, in API order: `{"document", "job_id", "status", "metadata", "usage",
  "steps": [{"index", "name", "type", "step_id", "content": [...]}], "by_step":
  {name: [content...]}}`. Step names come from the API's `model` key (the old
  `step_index` / `step_id` on content entries are null on the live API and are
  no longer relied on). `content[].text` and `content[].additional_values` are
  parsed from their JSON strings. Split children are never merged or dropped —
  a step that ran per child has several entries, and `by_step` is a list per
  name. Validate lanes and merge provenance come through as ordinary steps.
- `run_agent.py --include` defaults to `all`; `last` remains available.
  `create_job()` accepts a `metadata` dict.
- `upstage_batch.py` (agent mode): the same generic model — `steps[]` in API
  order plus `extracted` as a **list** of `{data, additional_values}` per step
  name (previously split children overwrote each other). `final_output` is the
  last step's single value, or a list when that step ran per split child
  (previously only the first child). `--include` accepts only `all` (default)
  or `last`; the advertised `output:parse|classify|extract|instruct` filters
  were never accepted by `GET /v2/responses/{id}` and are removed.
- `score.py` reads the new shapes (and still reads the old ones).

**Repo delivery**

- The batch CLI now lives once, committed, at
  `skill/upstage-studio/scripts/upstage_batch.py`; `core/` is gone and
  `build.sh` stamps the skill copy into `gui/`. A plain `git clone` is complete.

**Docs**

- New `INTEGRATION_QUICKSTART.md` (HTTP-first, language-agnostic).
- `references/agent-api.md` rewritten: per-step output shapes for all six step
  types, a worked multi-step example, `metadata`, retention and deletion,
  jobs-list `include` polarity, `include[]=last|all` only on responses, no
  webhooks, undocumented rate limits stated as such.
- `references/schema-guide.md`: the step envelope, parse-step settings,
  `split_criteria`, the instruct `data.input` shape (not `prompt`), and new
  merge / validate sections.
- `SKILL.md` and `README.md` rewritten customer-first; scoring and schema
  engineering moved under "For POC evaluation". API key wording unified
  (create it in the Upstage Console; `Authorization: Bearer <key>`). Support:
  open an issue or contact your Upstage representative.

## 2.5.0 — 2026-09

- `score.py` gains `--extractions-dir` and `--run-label`; reports open with a
  `SCOPE:` line; `tn_policy` is required.
- Measurement guidance: n ≥ 3 runs, score on the deployable model, get the
  answer key first.
- Schema guide: size limit, generic-examples rule, `json_schema` vs
  `response_format` envelope, when to stop editing descriptions.
