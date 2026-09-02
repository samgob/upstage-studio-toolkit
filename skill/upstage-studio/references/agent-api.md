# Upstage Studio — Agent API Reference

Everything a Studio Agent does is available over the API at base URL
`https://api.upstage.ai/v2`. Authenticate every request with a bearer token:

```
Authorization: Bearer $UPSTAGE_API_KEY
```

API keys start with `up_` and are created in the Studio console.

Running a job is three calls: **upload a file → create a job → get the results.**
`scripts/run_agent.py` does all three; the reference below is for when you wire
the flow into your own code.

---

## 1. Upload a file — `POST /v2/files`

`multipart/form-data`:

| Field | Required | Notes |
|-------|----------|-------|
| `file` | yes | The document |
| `purpose` | no | Default `user_data` |
| `expires_after[seconds]` | no | Default 30 days |

Returns `{ "id": "file_...", "object": "file", ... }`.

After upload, page-image conversion runs automatically. Check readiness with
`GET /v2/files/{file_id}?view=status` — the `status` field goes
`PROCESSING → UPLOADED` (`UPLOADED` or `READY` means it's job-ready). Creating a
job against a file that is still `PROCESSING` returns `409`, so wait for
`UPLOADED` first.

**Supported formats:** jpg, png, bmp, tiff, heic, pdf, doc, docx, ppt, pptx,
xls, xlsx, hwp, hwpx. **Max 500 MB / 1,000 pages.**

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
  "include": ["all"]
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `model` | yes | Your Agent ID (`agt_...`) |
| `input` | yes | One or more input items |
| `config_id` | no | Pin an exact config version. Accepts `cfg_...` **or** its version number (`"1"`, `"2"`, …). Omit → the Agent's current default config runs. |
| `background` | no | Auto-true for `agt_` agents; then poll for results |
| `include` | no | `["last"]` (default, final step only) or `["all"]` (every step's output) |

**Three ways to supply the file** (pick one inside `content[]`):

| Method | Field |
|--------|-------|
| Uploaded file | `"file_id": "file_XXX"` |
| Public URL | `"file_url": "https://..."` (auto-downloaded) |
| Inline base64 | `"file_data": "data:application/pdf;base64,...", "filename": "doc.pdf"` |

Returns `{ "id": "job_...", "status": "in_progress" | "completed", ... }`.

---

## 3. Get the results — `GET /v2/responses/{job_id}`

Add the include parameter as a **bracketed array** to get all steps:

```
GET /v2/responses/{job_id}?include[]=all
```

`include[]=all` returns every step's output; omit it (or use `include[]=last`)
for just the final step. Always bracket it — `include[]=all` — so the array is
parsed correctly.

**Response shape:**

```json
{
  "id": "job_XXX",
  "status": "completed",
  "output": [
    { "type": "message", "role": "assistant",
      "content": [
        { "type": "output_text", "text": "...", "step_id": "step_XXX", "step_index": 0 }
      ] }
  ],
  "usage": { "total_tokens": 0 },
  "metadata": { "cached": "false" }
}
```

Each step's result is in `content[].text` (a JSON string for classify/extract
steps — parse it to get the structured object). `step_index` tells you the
order (parse → classify → extract → instruct).

Statuses: `in_progress`, `completed`, `failed`.

---

## Listing past jobs — `GET /v2/agents/{agent_id}/jobs`

| Parameter | Notes |
|-----------|-------|
| `config_id` | Filter to one config version |
| `source` | `api` or `studio` |
| `include[]` | `output` (all steps) or a single step: `output:parse`, `output:classify`, `output:extract`, `output:instruct`. Multiple allowed. |
| `after` / `before` / `limit` / `order` | Pagination |

Results are paginated (with `first_id`, `last_id`, `has_more`) and returned in
ascending order — request a larger `limit` and/or walk the cursors to reach the
newest jobs.

---

## Configs and the default — `GET /v2/agents/{agent_id}/configs`

Every edit to a workflow creates a new immutable config version, so "updating"
a step means creating a new config (clone the current one, swap in the new
schema) and, when you're happy with it, marking it the Agent's default with
`is_default: true` — which un-sets whichever config held it before.

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

## Error codes

A `failed` job carries `error.code` and `error.message`:

| Code | Meaning | Action |
|------|---------|--------|
| `parse_error` | Parsing failed | Try a different format or re-upload |
| `preprocess_error` | Preprocessing failed | Check the file isn't corrupted |
| `classify_error` | Classification failed | Retry |
| `extract_error` | Extraction failed | Reduce pages or simplify the schema |
| `instruct_error` | Instruct step failed | Retry |
| `job_timeout_error` | Over the 1-hour processing cap | Reduce pages / split |
| `server_error` | Transient server issue | Retry |

HTTP-level: `409` = file still converting (wait for `UPLOADED`); `415` =
unsupported file type; `429` = rate limit (the scripts back off and retry
automatically, honoring `Retry-After`).

---

## Caching

Identical file + identical step settings reuse prior results for **7 days**.
`metadata.cached` is `"true"` when every step was served from cache. To force a
fresh run, change any step setting (or the schema) so the inputs differ.

This matters when you re-run a config to measure run-to-run variation: two runs
that return byte-identical output are one draw served twice, not two draws.
Change something about the input before you re-run, and treat `metadata.cached`
as a hint rather than proof — the output itself is the evidence.
