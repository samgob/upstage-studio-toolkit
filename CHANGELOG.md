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
  `create_job()` accepts a `metadata` dict — the API accepts it but does
  **not** return it on reads (only server-set keys such as `source` come
  back), so results are keyed by filename, not by metadata.
- **Partial output on failure.** A `failed` job still returns every step that
  completed, each `output[]` item with its own `status`, and `error.step`
  names the failing step. `run_agent.py` now returns the failed job instead of
  raising, writes `<stem>.json` with `status: "failed"`, `error` and the
  partial `steps` / `by_step` (per-step `status` included), exits non-zero for
  that document and keeps going on folder runs. `upstage_batch.py` keeps the
  partial `steps` / `extracted` on a failed job and records `error_code` /
  `error_step` / `api_error`; `metadata` is carried on every result.
- `upstage_batch.py` (agent mode): the same generic model — `steps[]` in API
  order plus `extracted` as a **list** of `{data, additional_values}` per step
  name (previously split children overwrote each other). `final_output` is the
  last step's single value; when that step ran per split child the entries
  are collapsed to one value if they are all equal (post-merge instruct /
  validate entries are identical) and kept as a list otherwise (previously
  only the first child). `--include` accepts only `all` (default) or `last`;
  the advertised `output:parse|classify|extract|instruct` filters were never
  accepted by `GET /v2/responses/{id}` and are removed.
- `run_agent.py` keeps a non-JSON `additional_values` string as-is (was
  dropped to `null`), waits up to ~300 s for file conversion, and retries a
  `409` on job create once. `upstage_batch.py` does the same.
- `score.py` reads the new shapes (and still reads the old ones).

**GUI**

- `gui/upstage_batch_gui.py` resolves `upstage_batch.py` from a third
  location, `skill/upstage-studio/scripts/`, so a fresh clone runs the GUI
  without a build step (the zip still ships the script next to the GUI).
  Version stamped 2.6.0.

**Repo delivery**

- The batch CLI now lives once, committed, at
  `skill/upstage-studio/scripts/upstage_batch.py`; `core/` is gone and
  `build.sh` stamps the skill copy into `gui/`. A plain `git clone` is complete.

**Docs**

- New `INTEGRATION_QUICKSTART.md` (HTTP-first, language-agnostic).
- `references/agent-api.md` rewritten: per-step output shapes for all six step
  types, a worked multi-step example, `metadata` (accepted, not returned),
  `metadata.cached` may be absent (use per-step `cache_hit`), partial output
  on failure, `invalid_request_error`, job-duration expectations, retention
  and deletion, jobs-list `include` polarity, `include[]=last|all` only on
  responses, no webhooks, undocumented rate limits stated as such.
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
