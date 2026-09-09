#!/usr/bin/env python3
"""
run_agent.py — Run documents through an Upstage AI Studio Agent via the API.

Handles the full job flow (upload -> create -> poll -> retrieve) for a single
file or an entire folder, with parallel workers and automatic retry on rate
limits. Standard library only; no dependencies to install.

Examples
--------
  # One document; every step's output is included by default
  python run_agent.py --agent agt_XXX --config-id cfg_XXX --input invoice.pdf

  # A whole folder, 5 in parallel, save one JSON per document to results/
  python run_agent.py --agent agt_XXX --config-id cfg_XXX \
      --input ./invoices/ --workers 5 --output results/

  # Only the final step's output
  python run_agent.py --agent agt_XXX --input invoice.pdf --include last

Output shape (one JSON per document)
------------------------------------
  {
    "document": "invoice.pdf",
    "job_id": "job_XXX",
    "status": "completed",
    "metadata": {"cached": "false", ...},
    "usage": {...},
    "steps": [                      # every step, in API order
      {"index": 0, "name": "parse", "type": "document-parse", "step_id": "...",
       "content": [
          {"text": <parsed JSON, or the raw string>,
           "additional_values": <parsed object, or null>,
           ...any other keys the API returned on that content entry}
      ]},
      ...
    ],
    "by_step": {"parse": [content...], "classify": [content...], ...}
  }

  A step that ran once per split child (e.g. an extract after a splitting
  classify) has several entries in its "content" list. Nothing is merged or
  dropped — the file mirrors what the API returned, with JSON strings parsed.

Environment
-----------
  UPSTAGE_API_KEY   Your Upstage API key (create one in the Upstage Console). Or pass --key.
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error

BASE_URL = "https://api.upstage.ai/v2"

SUPPORTED_EXT = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".pdf",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".hwp", ".hwpx",
}


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib urllib, with retry on 429 / transient server errors)
# --------------------------------------------------------------------------- #

def _request(method, url, api_key, *, json_body=None, multipart=None,
             max_retries=6):
    """Make one HTTP request, retrying on 429 and 5xx with backoff."""
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None

    if multipart is not None:
        boundary = f"----upstage{uuid.uuid4().hex}"
        data = _encode_multipart(multipart, boundary)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        attempt += 1
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except error.HTTPError as e:
            status = e.code
            retry_after = e.headers.get("Retry-After") if e.headers else None
            body = e.read().decode("utf-8", "replace")
            if status in (429, 500, 502, 503, 504) and attempt <= max_retries:
                wait = float(retry_after) if retry_after else min(2 ** attempt, 60)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {status} on {method} {url}: {body}") from None
        except (error.URLError, TimeoutError) as e:
            if attempt <= max_retries:
                time.sleep(min(2 ** attempt, 60))
                continue
            raise RuntimeError(f"Network error on {method} {url}: {e}") from None


def _encode_multipart(fields, boundary):
    """Encode a small multipart/form-data body. fields: dict of name->value,
    where a value that is a (filename, bytes) tuple is sent as a file part."""
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, content = value
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            out += (f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n').encode()
            out += f"Content-Type: {ctype}\r\n\r\n".encode()
            out += content
            out += b"\r\n"
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += f"{value}\r\n".encode()
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


# --------------------------------------------------------------------------- #
# The three-step job flow
# --------------------------------------------------------------------------- #

def upload_file(path, api_key):
    """POST /v2/files, then wait until the file is UPLOADED (job-ready)."""
    with open(path, "rb") as f:
        content = f.read()
    resp = _request("POST", f"{BASE_URL}/files", api_key,
                    multipart={"file": (os.path.basename(path), content),
                               "purpose": "user_data"})
    file_id = resp["id"]

    # Image conversion runs after upload; poll until the file is job-ready.
    for _ in range(60):
        info = _request("GET", f"{BASE_URL}/files/{file_id}?view=status", api_key)
        status = str(info.get("status", "")).upper()
        if status in ("UPLOADED", "READY"):
            return file_id
        if status == "FAILED":
            raise RuntimeError(f"File conversion failed for {path}")
        time.sleep(2)
    return file_id  # proceed anyway; job creation will report 409 if not ready


def create_job(agent_id, file_id, api_key, *, config_id=None, include="all",
               metadata=None):
    """POST /v2/responses. Returns (job_id, raw response).

    `metadata` is an optional dict of your own key/values (e.g. your document
    ID) that comes back on GET /v2/responses/{job_id} under "metadata"."""
    body = {
        "model": agent_id,
        "input": [{"role": "user",
                   "content": [{"type": "input_file", "file_id": file_id}]}],
        "background": True,
        "include": [include],   # "all" (every step) or "last" (final step only)
    }
    if config_id:
        body["config_id"] = config_id
    if metadata:
        body["metadata"] = metadata
    resp = _request("POST", f"{BASE_URL}/responses", api_key, json_body=body)
    return resp["id"], resp


def get_job(job_id, api_key, *, include="all"):
    """GET /v2/responses/{job_id}. NOTE the bracketed array form ?include[]=all
    (the unbracketed ?include=all is ignored and you get only the last step)."""
    url = f"{BASE_URL}/responses/{job_id}?include[]={include}"
    return _request("GET", url, api_key)


def poll_job(job_id, api_key, *, include="all", timeout=2400, interval=4):
    """Poll a job until it completes or fails. Polling is the only completion
    signal — the API has no webhooks."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get_job(job_id, api_key, include=include)
        status = job.get("status")
        if status == "completed":
            return job
        if status == "failed":
            err = job.get("error") or {}
            raise RuntimeError(f"Job {job_id} failed: "
                               f"{err.get('code')} — {err.get('message')}")
        time.sleep(interval)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


def run_one(path, agent_id, api_key, *, config_id=None, include="all",
            metadata=None):
    """Full flow for a single document. Returns the completed job dict."""
    file_id = upload_file(path, api_key)
    job_id, _ = create_job(agent_id, file_id, api_key,
                           config_id=config_id, include=include,
                           metadata=metadata)
    return poll_job(job_id, api_key, include=include)


# --------------------------------------------------------------------------- #
# Output shaping
# --------------------------------------------------------------------------- #

def _parse_json_maybe(value):
    """Return json.loads(value) when value is a string holding JSON, else value."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def shape_job(job, document=None):
    """Turn a completed job into the per-document JSON this script writes.

    The API returns one item in `output[]` per step that ran, in execution
    order; the step's name is the item's `model` key and its type (document-parse,
    document-classify, information-extract, instruct, merge, validate) is the
    item's `step_type` key. Each item carries a
    `content[]` list with one entry per result — several when the step ran
    once per split child. `text` is the step's output (a JSON string for
    classify / extract / validate / merge; free text for instruct; the parsed
    document JSON, with content.html inside, for parse) and `additional_values` is a JSON *string* holding per-step
    extras (classify confidence and page ranges, validate check trace,
    merge provenance). Both are parsed here when they parse.

    No assumptions are made about how many steps ran or what types they are.
    """
    steps = []
    by_step = {}
    for i, item in enumerate(job.get("output") or []):
        name = item.get("model") or f"step_{i}"
        contents = []
        for part in item.get("content") or []:
            entry = dict(part)
            entry["text"] = _parse_json_maybe(part.get("text"))
            av = _parse_json_maybe(part.get("additional_values"))
            entry["additional_values"] = av if isinstance(av, (dict, list)) else None
            contents.append(entry)
        steps.append({"index": i, "name": name, "type": item.get("step_type"),
                      "step_id": item.get("step_id"), "content": contents})
        by_step.setdefault(name, []).extend(contents)   # list per name, never overwrite

    shaped = {
        "document": document,
        "job_id": job.get("id"),
        "status": job.get("status"),
        "metadata": job.get("metadata"),
        "usage": job.get("usage"),
        "steps": steps,
        "by_step": by_step,
    }
    if job.get("error"):
        shaped["error"] = job["error"]
    return shaped


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def gather_inputs(input_path):
    if os.path.isfile(input_path):
        return [input_path]
    if os.path.isdir(input_path):
        files = []
        for name in sorted(os.listdir(input_path)):
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXT:
                files.append(os.path.join(input_path, name))
        return files
    raise SystemExit(f"Input not found: {input_path}")


def main():
    p = argparse.ArgumentParser(description="Run documents through an Upstage Studio Agent.")
    p.add_argument("--agent", required=True, help="Agent ID (agt_...)")
    p.add_argument("--input", required=True, help="A file or a folder of documents")
    p.add_argument("--config-id", default=None,
                   help="Config ID (cfg_...) or version number to pin. "
                        "Omit to run the Agent's default config.")
    p.add_argument("--key", default=os.environ.get("UPSTAGE_API_KEY"),
                   help="API key (or set UPSTAGE_API_KEY)")
    p.add_argument("--include", choices=["all", "last"], default="all",
                   help="'all' (default) = every step's output; "
                        "'last' = final step only")
    p.add_argument("--workers", type=int, default=5,
                   help="Parallel jobs for folder runs (default 5)")
    p.add_argument("--output", default=None,
                   help="Folder to write one <doc>.json result per document")
    args = p.parse_args()

    if not args.key:
        raise SystemExit("No API key. Pass --key or set UPSTAGE_API_KEY.")

    inputs = gather_inputs(args.input)
    if not inputs:
        raise SystemExit("No supported documents found.")

    if args.output:
        os.makedirs(args.output, exist_ok=True)

    print(f"Running {len(inputs)} document(s) through {args.agent}"
          + (f" @ {args.config_id}" if args.config_id else "")
          + f"  (include={args.include})\n")

    results, failures = {}, {}

    def work(path):
        job = run_one(path, args.agent, args.key,
                      config_id=args.config_id, include=args.include)
        return path, job

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(work, path): path for path in inputs}
        for fut in as_completed(futures):
            path = futures[fut]
            name = os.path.basename(path)
            try:
                _, job = fut.result()
                shaped = shape_job(job, document=name)
                results[name] = shaped
                cached = (job.get("metadata") or {}).get("cached")
                tag = " (cached)" if cached == "true" else ""
                names = ", ".join(st["name"] for st in shaped["steps"])
                print(f"  [ok] {name} — {len(shaped['steps'])} step(s){tag}: {names}")
                if args.output:
                    stem = os.path.splitext(name)[0]
                    with open(os.path.join(args.output, f"{stem}.json"), "w") as f:
                        json.dump(shaped, f, indent=2, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001 — report, keep going
                failures[name] = str(e)
                print(f"  [fail] {name} — {e}")

    print(f"\nDone. {len(results)} succeeded, {len(failures)} failed.")

    # For a single file with no --output, print the result to stdout.
    if len(inputs) == 1 and not args.output and results:
        print("\n" + json.dumps(next(iter(results.values())), indent=2,
                                 ensure_ascii=False))

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
