# Upstage Studio — Agent API Reference

Everything a Studio Agent does is available over the API at base URL
`https://api.upstage.ai/v2`. Authenticate every request with a bearer token:

```
Authorization: Bearer <your API key>
```

Create the key in the Upstage Console (console.upstage.ai). There are no
webhooks — you poll for completion (section 3).

Running a job is three calls: **upload a file → create a job → poll for the
results.** `scripts/run_agent.py` does all three; the reference below is for
when you wire the flow into your own code. For a language-agnostic walkthrough
with curl, see `INTEGRATION_QUICKSTART.md` at the repo root.

---

## 1. Upload a file — `POST /v2/files`

`multipart/form-data`:

| Field | Required | Notes |
|-------|----------|-------|
| `file` | yes | The document |
| `purpose` | no | Default `user_data` |
| `expires_after[anchor]` | no | `created_at` |
| `expires_after[seconds]` | no | Seconds until the file expires. **Default 30 days.** |

Returns `{ "id": "file_XXX", "object": "file", "bytes": ..., "filename": ..., "created_at": ..., "expires_at": ... }`.

After upload, page-image conversion runs automatically. Check readiness with
`GET /v2/files/{file_id}?view=status` — the `status` field goes
`PROCESSING → UPLOADED` (`UPLOADED` or `READY` means it's job-ready; `FAILED`
means conversion failed). Creating a job against a file that is still
`PROCESSING` returns `409`, so wait for `UPLOADED` first.

**Supported formats:** jpg, png, bmp, tiff, heic, pdf, doc, docx, ppt, pptx,
xls, xlsx, hwp, hwpx. **Max 500 MB / 1,000 pages.**

**Delete:** `DELETE /v2/files/{file_id}` → `{ "id": "file_XXX", "object": "file", "deleted": true }`.
`GET /v2/files` lists files (cursor pagination).

---

## 2. Create a job — `POST /v2/responses`

```json
{
  "model": "agt_XXX",
  "input": [
    { "role": "user", "content": [ { "type": "input_file", "file_id": "file_XXX" } ] }
  ],
  "config_id": "cfg_XXX",
  "background": true,
  "include": ["all"],
  "metadata": { "your_document_id": "INV-000123" }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `model` | yes | Your Agent ID (`agt_...`) |
| `input` | yes | One or more input items |
| `config_id` | no | Pin an exact config version. Accepts `cfg_...` **or** its version number (`"1"`, `"2"`, …). Omit → the Agent's current default config runs. |
| `background` | no | Auto-true for `agt_` agents; then poll for results |
| `include` | no | `["last"]` (default, final step only) or `["all"]` (every step's output) |
| `metadata` | no | A JSON object of your own key/values. It is returned unchanged on `GET /v2/responses/{job_id}` (alongside the server's own `source` and `cached` keys), so use it to carry your document ID or correlation ID. |

**Three ways to supply the file** (pick one per `content[]` item):

| Method | Field |
|--------|-------|
| Uploaded file | `"file_id": "file_XXX"` |
| URL | `"file_url": "https://..."` — the URL must be fetchable by Upstage's servers (no private network, no auth-gated link) |
| Inline base64 | `"file_data": "data:application/pdf;base64,...", "filename": "doc.pdf"` |

**Several files in one job.** `content[]` may hold more than one `input_file`
item; they run as one job and the results carry each file's boundaries. One
job per document is the simpler pattern when you need one result per document.

Returns `{ "id": "job_XXX", "status": "in_progress" | "completed", ... }`.

---

## 3. Poll for the results — `GET /v2/responses/{job_id}`

```
GET /v2/responses/{job_id}?include[]=all
```

Poll until `status` is `completed` or `failed`. **This is the only completion
signal — the API does not call you back.** A sensible loop polls every few
seconds with a ceiling of the server's own 1-hour job timeout.

`include[]` accepts exactly two values: `all` (every step's output) and `last`
(final step only, the default). Two things bite here:

- **Bracket it.** `?include[]=all` works; the unbracketed `?include=all` is
  silently ignored and you get only the last step.
- **Only `last` | `all`.** Per-step filters such as `include[]=output:extract`
  are for the jobs-list endpoint (section 5) and return `400` here.

**Response shape:**

```json
{
  "id": "job_XXX",
  "status": "completed",
  "model": "agt_XXX",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "model": "parse",
      "step_type": "document-parse",
      "step_id": "...",
      "status": "completed",
      "content": [
        { "type": "output_text", "text": "...", "additional_values": "{...}" }
      ]
    }
  ],
  "usage": { "input_tokens": 0, "output_tokens": 0, "total_tokens": 0 },
  "metadata": { "source": "api", "cached": "false", "your_document_id": "INV-000123" },
  "created_at": 1700000000
}
```

How to read `output[]`:

- **One item per step that ran, in execution order.** A step that ran on
  several branches (an extract after a splitting classify) appears **once per
  branch**, as separate items with the same `model` name — key on the name and
  collect a list, don't assume one item per name.
- **`model` on the item is the step's name** (as set in the config).
  **`step_type`** is the step's type (`document-parse`, `document-classify`,
  `information-extract`, `instruct`, `merge`, `validate`). Ignore
  `content[].step_id` / `content[].step_index` — they are null.
- **`content[]` holds the results.** One entry when the step ran once. After a
  splitting classify, steps that run **once per split child** return one entry
  per child under a single item — the classify step itself, and any instruct or
  validate downstream of a `merge`. (Per-branch extracts, by contrast, arrive as
  separate items.) Always iterate `content[]`.
- **`content[].text`** is the step's output. For classify, extract, validate
  and merge it is a JSON string — parse it. For instruct it is the model's
  reply (JSON only if you set a `text.format` on the step). For parse it is
  the JSON of the parsed document.
- **`content[].additional_values`** is a **JSON string** (parse it) of per-step
  extras — see the table below. Its `page_ranges` tells you which pages of the
  input a split child covers; `previous_step_name` tells you which step fed it.

Statuses: `in_progress`, `completed`, `failed`. A `failed` job carries
`error.code` and `error.message` (section 7).

**Delete:** `DELETE /v2/responses/{job_id}` → `{ "id": "job_XXX", "object": "job", "deleted": true }`.

### What each step type returns

| Step type | `content[].text` | `content[].additional_values` (parsed) |
|-----------|------------------|----------------------------------------|
| `document-parse` | JSON: `{ "api", "model", "content": { "html", "text", "markdown" }, "elements": [...], "usage": { "pages" } }` — the full parsed document; `elements[]` carry per-element category, page and coordinates | `cache_hit`, `document_ids`, `source_file_boundaries` |
| `document-classify` | The predicted class label (a bare string, e.g. `certificate_of_analysis`). With `split: true`: **one `content[]` entry per split child** | `document_type: { _value, confidence_score (0–1), confidence: "high"\|"medium"\|"low" }`, `page_ranges: [[first, last], ...]`, `split_criteria_info`, `previous_step_name`, `cache_hit` |
| `information-extract` | JSON object of your schema's fields | One key per field: `{ _value, confidence: "high"\|"medium"\|"low", page, coordinates, word_coordinates }` (when `confidence` / `location` are on), plus `page_ranges`, `previous_step_name`, `cache_hit` |
| `instruct` | The model's answer as text — free prose unless the step has a `text.format`. After a split, one `content[]` entry per split child | `previous_step_name`, `page_ranges`, `cache_hit` |
| `merge` | JSON provenance only: `{ merge_step_name, count, arrived_count, skipped_count, source_steps: [{ branch_key, step_name, step_type }], page_ranges: [...] }` — no field values; read those from the extract items | `cache_hit` |
| `validate` | The lane: `green`, `yellow`, or `red` (sometimes JSON-encoded with quotes — strip them). After a split, one `content[]` entry per split child | `verdict`, `checks: [{ name, severity, passed, reason, conditions: {...} }]` — a readable per-check trace with the value each operand resolved to — plus `previous_step_name`, `page_ranges`, `cache_hit` |

Lane rule for validate: any failed `error`-severity check → `red`; only
`warning` checks failed → `yellow`; all pass → `green`. With a splitting
classify upstream, validate emits one result per split child.

### Worked example — a supplier certificate packet

Config: parse → classify (split into document types) → one extract per type →
merge → instruct (a disposition summary) → validate (completeness checks). A
three-page PDF holding a delivery note and two certificates of analysis
produces this (`text` / `additional_values` shown parsed, values abridged):

```json
{
  "id": "job_XXX",
  "status": "completed",
  "metadata": { "source": "api", "cached": "false", "your_document_id": "PKT-0001" },
  "output": [
    { "model": "parse", "step_type": "document-parse",
      "content": [ { "type": "output_text",
                     "text": { "api": "2.0", "content": { "html": "<p>...</p>", "text": "...", "markdown": "..." },
                               "elements": [ "..." ], "usage": { "pages": 3 } },
                     "additional_values": { "cache_hit": false, "document_ids": ["file_XXX"] } } ] },

    { "model": "classify", "step_type": "document-classify",
      "content": [
        { "type": "output_text", "text": "delivery_note",
          "additional_values": { "document_type": { "_value": "delivery_note", "confidence_score": 0.97, "confidence": "high" },
                                 "page_ranges": [[1, 1]], "previous_step_name": "parse" } },
        { "type": "output_text", "text": "certificate_of_analysis",
          "additional_values": { "document_type": { "_value": "certificate_of_analysis", "confidence_score": 0.94, "confidence": "high" },
                                 "page_ranges": [[2, 2]], "previous_step_name": "parse" } },
        { "type": "output_text", "text": "certificate_of_analysis",
          "additional_values": { "document_type": { "_value": "certificate_of_analysis", "confidence_score": 0.92, "confidence": "high" },
                                 "page_ranges": [[3, 3]], "previous_step_name": "parse" } } ] },

    { "model": "extract_delivery_note", "step_type": "information-extract",
      "content": [ { "type": "output_text",
                     "text": { "supplier_name": "Example Chemicals Ltd", "delivery_note_number": "DN-1001", "purchase_order_number": "4500000001" },
                     "additional_values": { "page_ranges": [[1, 1]], "previous_step_name": "classify",
                                            "supplier_name": { "_value": "Example Chemicals Ltd", "confidence": "high", "page": 1, "coordinates": [ "..." ] },
                                            "delivery_note_number": { "_value": "DN-1001", "confidence": "high", "page": 1, "coordinates": [ "..." ] } } } ] },

    { "model": "extract_certificate_of_analysis", "step_type": "information-extract",
      "content": [ { "type": "output_text",
                     "text": { "supplier_name": "Example Chemicals Ltd", "lot_number": "L-2201", "results": [ { "property": "Viscosity", "value": "410", "unit": "mPa.s" } ] },
                     "additional_values": { "page_ranges": [[2, 2]], "previous_step_name": "classify", "lot_number": { "_value": "L-2201", "confidence": "high", "page": 2 } } } ] },

    { "model": "extract_certificate_of_analysis", "step_type": "information-extract",
      "content": [ { "type": "output_text",
                     "text": { "supplier_name": "Example Chemicals Ltd", "lot_number": "L-2202", "results": [ { "property": "Viscosity", "value": "", "unit": "" } ] },
                     "additional_values": { "page_ranges": [[3, 3]], "previous_step_name": "classify", "lot_number": { "_value": "L-2202", "confidence": "high", "page": 3 } } } ] },

    { "model": "merge_all", "step_type": "merge",
      "content": [ { "type": "output_text",
                     "text": { "merge_step_name": "merge_all", "count": 3, "arrived_count": 3, "skipped_count": 0,
                               "source_steps": [ { "branch_key": "0:delivery_note", "step_name": "extract_delivery_note", "step_type": "information-extract" },
                                                 { "branch_key": "1:certificate_of_analysis", "step_name": "extract_certificate_of_analysis", "step_type": "information-extract" },
                                                 { "branch_key": "2:certificate_of_analysis", "step_name": "extract_certificate_of_analysis", "step_type": "information-extract" } ],
                               "page_ranges": [ { "branch_key": "0:delivery_note", "ranges": [[1, 1]] }, { "branch_key": "1:certificate_of_analysis", "ranges": [[2, 2]] }, { "branch_key": "2:certificate_of_analysis", "ranges": [[3, 3]] } ] },
                     "additional_values": { "cache_hit": false } } ] },

    { "model": "disposition", "step_type": "instruct",
      "content": [ { "type": "output_text",
                     "text": "Delivery DN-1001 covers lots L-2201 and L-2202. Lot L-2202's certificate reports no viscosity result; hold pending a corrected certificate.",
                     "additional_values": { "previous_step_name": "merge_all" } } ] },

    { "model": "completeness_gate", "step_type": "validate",
      "content": [ { "type": "output_text", "text": "yellow",
                     "additional_values": { "verdict": "yellow", "previous_step_name": "merge_all",
                                            "checks": [
                                              { "name": "lot number present", "severity": "error", "passed": true,
                                                "reason": "all-branches(extract_certificate_of_analysis.lot_number)=['L-2201', 'L-2202'] filled → pass" },
                                              { "name": "purchase order present", "severity": "warning", "passed": true,
                                                "reason": "all-branches(extract_delivery_note.purchase_order_number)=['4500000001'] filled → pass" },
                                              { "name": "viscosity reported", "severity": "warning", "passed": false,
                                                "reason": "..." } ] } } ] }
  ]
}
```

Things to notice:

- The two certificates come back as **two separate `extract_certificate_of_analysis`
  items**, while the classify step lists all three children in one item. Key by
  step name and collect lists. (The instruct and validate steps are shown with
  one entry each for brevity; on a live split job they carry one entry per
  child, each with its own `page_ranges`.)
- `page_ranges` is how you tie a child result back to the pages of the file.
- The validate verdict is `yellow` because only a `warning` check failed; a
  downstream `next_steps` condition on `{"field": "text", "operator": "==", "value": "yellow"}`
  would route this job to review.
- With `include[]=last` you would receive only the final item.

---

## 4. Retention, expiry and deletion

| Object | Retention | Control |
|--------|-----------|---------|
| Files | Expire **30 days** after upload by default | `expires_after[seconds]` at upload; `DELETE /v2/files/{id}` any time |
| Jobs (responses) | Kept per the Agent's `expires_after` policy (seconds; `null` = no expiry) | Set on `POST /v2/agents` / `PATCH /v2/agents/{id}`; `DELETE /v2/responses/{id}` any time |

If you need results gone on your own schedule, delete the job and the file
after you have stored the output.

---

## 5. Listing past jobs — `GET /v2/agents/{agent_id}/jobs`

| Parameter | Notes |
|-----------|-------|
| `config_id` | Filter to one config version |
| `source` | `api` or `studio` |
| `include` | **Unbracketed** here: `include=output` (all steps) or one step type — `include=output:parse`, `output:classify`, `output:extract`, `output:instruct`, `output:merge`, `output:validate`. Repeat the parameter for several types: `include=output:extract&include=output:validate`. Omit for no output. |
| `after` / `before` / `limit` / `order` | Pagination |

Two traps: the **bracketed** form `include[]=…` is silently ignored on this
endpoint (HTTP 200, `output: null`), and a comma-joined list returns an empty
output. This is the opposite polarity from `/v2/responses/{job_id}`, which
wants `include[]=`.

Results are paginated (with `first_id`, `last_id`, `has_more`) and returned in
ascending order — request a larger `limit` and/or walk the cursors to reach the
newest jobs. Each item has `id`, `config_id`, `status`, `error`, `files`,
`cached`, `metadata`, `output`, `created_at`, `updated_at`.

`GET /v2/stats/jobs` (filters `agent_id`, `config_id`, `source`, `since`)
returns counts of `in_progress` / `completed` / `failed` / `cached` jobs.

---

## 6. Configs and the default — `GET /v2/agents/{agent_id}/configs`

A config is the workflow: a list of steps, each with the same envelope —

```json
{ "name": "extract_invoice", "type": "information-extract", "data": { "...": "..." },
  "is_first": false, "next_steps": [ { "step_name": "check_totals" } ] }
```

Six step types: `document-parse`, `document-classify`, `information-extract`,
`instruct`, `merge`, `validate`. Exactly one step is `is_first` (the parse
step); `next_steps` entries may carry a `condition` (`{"field": "text",
"operator": "==", "value": "..."}` on a classify label or a validate lane) and
an entry without one is the fallback. `POST /v2/agents/{agent_id}/configs`
creates a config; `GET .../configs/{config_id}` reads one (`cfg_...` or the
version number). Step settings per type — including the instruct prompt's
`data.input` shape, `split_criteria`, and validate `checks` — are in
`schema-guide.md`.

One rule worth repeating: **unknown keys inside a step's `data` are accepted
and stored at create time and only fail when a job runs.** A config that saved
is not a config that works — run one job.

Every edit to a workflow creates a new immutable config version, so "updating"
a step means creating a new config (clone the current one, swap in the new
schema) and, when you're happy with it, marking it the Agent's default with
`is_default: true` (`POST .../configs/{config_id}` accepts only `name` and
`is_default`) — which un-sets whichever config held it before.

Two things follow from that, and they bite during a testing push:

- **Name experiments, and clean them up.** Iterating on a schema produces
  configs fast. A prefix (`test_`, `diag_`) keeps them identifiable, and
  deleting them once the decision is made keeps the list readable — thirty
  same-named configs is a list nobody can pick the champion out of.
- **Confirm the default after any batch of creates.** `GET` the config list and
  check that the config carrying `is_default: true` is the one you intend, and
  do it before anyone runs the Agent from the Studio UI. API jobs run whatever
  `config_id` you pin, so they're unaffected — but a UI run takes the default,
  and that's how a UI run and an API run end up disagreeing about a workflow
  that "hasn't changed". Treat the default as something you set explicitly and
  verify, not something you infer.

---

## 7. Errors, retries and limits

A `failed` job carries `error.code`, `error.message`, and (for step failures) `error.step`:

| Code | Meaning | Action |
|------|---------|--------|
| `parse_error` | Parsing failed | Try a different format or re-upload |
| `preprocess_error` | Preprocessing failed | Check the file isn't corrupted |
| `classify_error` | Classification failed | Retry |
| `extract_error` | Extraction failed | Reduce pages or simplify the schema |
| `instruct_error` | Instruct step failed | Retry |
| `tool_execution_error` | Tool execution error | Retry |
| `timeout_error` | A model call timed out ("Request timed out. Please try again later."); `error.step` names the step | Retry the job |
| `job_timeout_error` | Over the 1-hour processing cap | Reduce pages / split |
| `server_error` | Transient server issue | Retry; contact support if persistent |

HTTP-level: `400` = invalid request (including `include[]=output:*` on
`/responses/{id}`); `404` = unknown ID; `409` = file still converting (wait for
`UPLOADED`); `415` = unsupported file type; `429` = rate limit.

**Rate limits are not documented.** Treat `429` as the signal: honour the
`Retry-After` header when present, back off exponentially otherwise, and keep
client-side concurrency modest (the scripts default to 5 parallel jobs). Ask
your Upstage representative for the limits that apply to your account. Also
retry `500`/`502`/`503`/`504` with backoff; treat `4xx` other than `429` as
bugs in the request, not transient.

Enhanced-mode extraction is limited to 50 pages ("This document exceeds the
50-page limit for Enhanced mode"); standard mode supports up to 1,000.

---

## 8. Caching

Identical file + identical step settings reuse prior results for **7 days**.
`metadata.cached` is `"true"` when every step was served from cache, and each
step's `additional_values.cache_hit` says whether *that* step was. To force a
fresh run, change any step setting (or the schema) so the inputs differ.

For an integration this is a feature — re-submitting the same document to the
same config is cheap and returns the same answer. For measurement it matters:
two runs that return byte-identical output are one draw served twice, not two
draws. Change something about the input before you re-run, and treat
`metadata.cached` as a hint rather than proof — the output itself is the
evidence.
