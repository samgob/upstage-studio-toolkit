# Integration Quickstart — calling a Studio Agent from your own application

This is the HTTP-level flow for running documents through an Upstage AI Studio
Agent from any language (.NET, Java, Node, Python, …). No SDK is required; every
call below is plain HTTPS + JSON. The Python scripts in `skill/upstage-studio/scripts/`
implement exactly this sequence if you want a reference to read alongside it.

Where something is not documented by Upstage (rate limits, SLAs), this page says
so rather than guessing — ask your Upstage representative for the figures that
apply to your account.

## 1. Prerequisites

- An Agent built in Studio ([studio.upstage.ai](https://studio.upstage.ai)) — note
  its **Agent ID** (`agt_…`) and the **Config ID** (`cfg_…`) or version number of
  the workflow version you tested. The Agent ID is in the agent's URL in Studio;
  the config version (and its `cfg_…` ID) is in the agent's version list.
- Outbound HTTPS to `https://api.upstage.ai`.
- Documents in a supported format: jpg, png, bmp, tiff, heic, pdf, doc, docx,
  ppt, pptx, xls, xlsx, hwp, hwpx — up to 500 MB and 1,000 pages each.

## 2. Create an API key

Sign in to the Upstage Console ([console.upstage.ai](https://console.upstage.ai))
and create an API key. Send it on every request as a bearer token:

```
Authorization: Bearer <your API key>
```

Keep it server-side. Rotate it from the Console if it is ever exposed.

## 3. Upload the file

```bash
curl -s https://api.upstage.ai/v2/files \
  -H "Authorization: Bearer $UPSTAGE_API_KEY" \
  -F "file=@invoice.pdf" \
  -F "purpose=user_data"
# → { "id": "file_XXX", "object": "file", "bytes": 102400, "filename": "invoice.pdf",
#     "created_at": 1700000000, "expires_at": 1702592000 }
```

Page-image conversion runs after upload. The file is usable once its status is
`UPLOADED`; creating a job while it is still `PROCESSING` returns **409**.

```bash
curl -s "https://api.upstage.ai/v2/files/file_XXX?view=status" \
  -H "Authorization: Bearer $UPSTAGE_API_KEY"
# → { ..., "status": "UPLOADED" }      (PROCESSING → UPLOADED; FAILED = conversion failed)
```

Alternatives to uploading: pass `"file_url": "https://…"` in the job (the URL
must be fetchable by Upstage's servers — public or pre-signed, not on your
private network) or `"file_data": "data:application/pdf;base64,…"` with a
`"filename"`.

## 4. Create the job

```bash
curl -s https://api.upstage.ai/v2/responses \
  -H "Authorization: Bearer $UPSTAGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agt_XXX",
    "config_id": "cfg_XXX",
    "input": [ { "role": "user", "content": [ { "type": "input_file", "file_id": "file_XXX" } ] } ],
    "background": true,
    "include": ["all"],
    "metadata": { "your_document_id": "INV-000123" }
  }'
# → { "id": "job_XXX", "status": "in_progress", ... }
#   ("metadata" is accepted here but not returned on reads — see below)
```

- `config_id` pins the exact workflow version. Omit it and the Agent's current
  default runs — fine for a demo, not for production.
- `include: ["all"]` returns every step's output; the default `["last"]` returns
  only the final step.
- `metadata` is accepted on job creation but, on the current API, is **not
  returned** on reads — only server-set keys such as `source` come back. Keep
  your own job-ID → document map; the reference scripts key results by
  filename.

## 5. Poll until done — there are no webhooks

The API does not call you back. Poll `GET /v2/responses/{job_id}` until
`status` is `completed` or `failed`:

```bash
curl -s "https://api.upstage.ai/v2/responses/job_XXX?include[]=all" \
  -H "Authorization: Bearer $UPSTAGE_API_KEY"
```

- Use the bracketed form `include[]=all`; it has worked consistently. The
  unbracketed form has been observed to be ignored (you then get only the last
  step). Send the brackets literally — a query builder that percent-encodes
  `[]` (as some .NET helpers do) is untested; build the query string by hand.
- `include[]` accepts only `all` or `last` on this endpoint; anything else
  (e.g. `output:extract`) is a **400**.
- Every few seconds is a reasonable interval. The server itself abandons a job
  after 1 hour (`job_timeout_error`), so cap your loop there.
- A `failed` job carries `error.code`, `error.message` and `error.step`, and
  still returns every step that completed (section 10).

**How long to expect.** A multi-step job with an instruct step commonly runs
one to several minutes — the instruct step dominates; `in_progress` for
60–120 s is normal, not a hang. Model calls that time out surface as
`timeout_error` with `error.step`; retry the job — steps that already completed
are served from cache on the retry.

## 6. Read the output — keyed by step name

The completed response holds `output[]`: **one item per step that ran, in
execution order.**

```json
{
  "id": "job_XXX",
  "status": "completed",
  "metadata": { "source": "api" },
  "output": [
    { "model": "parse",   "step_type": "document-parse",     "content": [ { "type": "output_text", "text": "{...}", "additional_values": "{...}" } ] },
    { "model": "classify","step_type": "document-classify",  "content": [ { "type": "output_text", "text": "invoice", "additional_values": "{...}" } ] },
    { "model": "extract_invoice", "step_type": "information-extract",
                                                             "content": [ { "type": "output_text", "text": "{\"invoice_number\": \"INV-000123\", \"total\": 1250.00}", "additional_values": "{...}" } ] },
    { "model": "check_totals", "step_type": "validate",      "content": [ { "type": "output_text", "text": "green", "additional_values": "{\"verdict\": \"green\", \"checks\": [...]}" } ] }
  ]
}
```

Rules for a robust reader:

1. The step's **name is `model`** on the output item and its **type is
   `step_type`**. Ignore `content[].step_id` and `content[].step_index` — they
   are null.
2. Key results by name and **collect a list per name**. A step that ran on
   several branches (an extract after a classify that split the file) appears
   once per branch as separate items with the same name.
3. `content[]` has one entry when the step ran once. After a splitting
   classify, steps that run once per split child — the splitting classify
   itself, and instruct / validate steps after a merge — return **one entry per
   child** under a single item. After a `merge`, instruct and validate return
   one `content[]` entry per split child and those entries are byte-identical
   (the step saw the merged context). Always iterate `content[]`.
4. `content[].text` is the step's output — a **JSON string** for classify,
   extract, validate and merge (parse it), free text for instruct, the parsed
   document JSON for parse.
5. `content[].additional_values` is also a **JSON string** — parse it for
   confidence scores, page ranges, field locations and validate traces.
6. Don't assume a fixed number or order of step types. Read what came back.

## 7. Per-step output shapes

| `step_type` | `text` (parsed) | `additional_values` (parsed) |
|-------------|-----------------|------------------------------|
| `document-parse` | `{ "content": { "html", "text", "markdown" }, "elements": [ … ], "usage": { "pages" } }` | `cache_hit`, `document_ids` |
| `document-classify` | The class label as a bare string; one `content[]` entry per split child | `document_type: { _value, confidence_score (0–1), confidence: "high"/"medium"/"low" }`, `page_ranges: [[first_page, last_page]]` |
| `information-extract` | Object of your schema's fields | Per field: `{ _value, confidence: "high"/"medium"/"low", page, coordinates, word_coordinates }` when the step's `confidence` / `location` settings are on (they default on); plus `page_ranges` |
| `instruct` | Free text (JSON only if the step sets a `text.format`) | `previous_step_name` |
| `merge` | Provenance only: which branches arrived (`source_steps[]`, `page_ranges[]`) — the values stay on the extract items | `cache_hit` |
| `validate` | `green` / `yellow` / `red` (may arrive JSON-quoted — strip the quotes) | `verdict`, `checks[]: { name, severity, passed, reason }` |

Validate lane rule: any failed `error` check → `red`; only `warning` checks
failed → `yellow`; all pass → `green`. Route on it in your own code, or let the
Agent's `next_steps` route on it server-side.

A full worked example of a parse → classify (split) → extract × N → merge →
instruct → validate response is in
`skill/upstage-studio/references/agent-api.md`.

## 8. Retention and deletion

| Object | Default retention | Your controls |
|--------|-------------------|---------------|
| Uploaded files | **30 days** (`expires_at` on the upload response) | `expires_after[seconds]` at upload; `DELETE /v2/files/{file_id}` |
| Jobs and their output | Per the Agent's `expires_after` setting (seconds; `null` = kept) | Set on the Agent; `DELETE /v2/responses/{job_id}` |

If your data-handling policy requires it, store the output on your side and
delete the job and file immediately:

```bash
curl -s -X DELETE https://api.upstage.ai/v2/responses/job_XXX -H "Authorization: Bearer $UPSTAGE_API_KEY"
curl -s -X DELETE https://api.upstage.ai/v2/files/file_XXX      -H "Authorization: Bearer $UPSTAGE_API_KEY"
# → { "id": "...", "object": "...", "deleted": true }
```

## 9. Caching

The same file (by content) run against the same config settings returns the
prior result for **7 days**. `metadata.cached` may be absent on completed jobs
(it was, even when steps were cache hits); treat a missing key as
not-fully-cached and rely on each step's `additional_values.cache_hit`, which
says whether that step came from cache. For an integration this is usually
welcome (a duplicate submission is
cheap and consistent). If you need a genuinely fresh run, change a step setting
or the config version.

## 10. Errors and retries

Job-level (`status: "failed"`, `error.code`):

| Code | Meaning | What to do |
|------|---------|------------|
| `parse_error` / `preprocess_error` | The file could not be parsed / preprocessed | Check the file; try another format |
| `classify_error` / `instruct_error` / `tool_execution_error` / `timeout_error` / `server_error` | Transient step failure (`error.step` names the step) | Retry the job |
| `extract_error` | Extraction failed (often page count) | Reduce pages or simplify the schema; enhanced mode is capped at 50 pages |
| `invalid_request_error` | The config is invalid at run time (`error.step` names the step) | Fix the config; don't retry |
| `job_timeout_error` | Exceeded the 1-hour cap | Split the document |

**A failed job still returns partial output.** `output[]` holds every step that
completed before the failure; each item carries its own `status`
(`completed` / `failed`) and `error.step` names the failing step. Observed:
`{"code": "timeout_error", "step": "instruct", "message": "Request timed out.
Please try again later."}` with 10 of 11 steps present. Persist what you got
before retrying — on the retry, steps that already completed are served from
cache.

HTTP-level: `400` invalid request · `404` unknown ID · `409` file still
converting (wait for `UPLOADED`) · `415` unsupported file type · `429` rate
limited · `5xx` transient.

Retry policy that works: on `429` honour the `Retry-After` header if present,
otherwise back off exponentially (e.g. 2 s, 4 s, 8 s … capped at 60 s); on
`5xx` back off the same way; do not retry other `4xx` — fix the request.

## 11. Concurrency

**Rate limits are not documented.** Until your Upstage representative gives you
the limits for your account, keep concurrency modest — a handful of jobs in
flight at once (the reference scripts default to 5) — and treat `429` as the
throttle signal. Each job's own processing time is dominated by the model
steps, so more parallelism mostly helps with queueing, not per-document
latency. Throughput and latency **SLAs are not documented** either; measure on
your own documents.

## 12. Several files in one job

`input[0].content[]` may hold more than one `input_file` item; they run as one
job and the parse step's `additional_values.source_file_boundaries` records
which file each part came from. If you need one result per document — the
usual case for a system of record — submit one job per file; it is simpler to
correlate (one job ID per document in your own map) and to retry.

---

Questions the references don't answer: open an issue on this repository or
contact your Upstage representative.
