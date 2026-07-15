#!/usr/bin/env python3
"""
run_agent.py — Run documents through an Upstage AI Studio Agent via the API.

Handles the full job flow (upload -> create -> poll -> retrieve) for a single
file or an entire folder, with parallel workers and automatic retry on rate
limits. Standard library only; no dependencies to install.

Examples
--------
  # One document, print every step's output
  python run_agent.py --agent agt_XXX --config-id cfg_XXX \
      --input invoice.pdf --include all

  # A whole folder, 5 in parallel, save each result to results/
  python run_agent.py --agent agt_XXX --config-id cfg_XXX \
      --input ./submissions/ --include all --workers 5 --output results/

Environment
-----------
  UPSTAGE_API_KEY   Your Upstage API key (starts with 'up_'). Or pass --key.
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


def create_job(agent_id, file_id, api_key, *, config_id=None, include="last"):
    """POST /v2/responses. Returns the job id."""
    body = {
        "model": agent_id,
        "input": [{"role": "user",
                   "content": [{"type": "input_file", "file_id": file_id}]}],
        "background": True,
        "include": [include],   # "last" or "all"
    }
    if config_id:
        body["config_id"] = config_id
    resp = _request("POST", f"{BASE_URL}/responses", api_key, json_body=body)
    return resp["id"], resp


def get_job(job_id, api_key, *, include="last"):
    """GET /v2/responses/{job_id}. NOTE the bracketed array form ?include[]=all."""
    url = f"{BASE_URL}/responses/{job_id}?include[]={include}"
    return _request("GET", url, api_key)


def poll_job(job_id, api_key, *, include="last", timeout=2400, interval=4):
    """Poll a job until it completes or fails."""
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


def run_one(path, agent_id, api_key, *, config_id=None, include="last"):
    """Full flow for a single document. Returns the completed job dict."""
    file_id = upload_file(path, api_key)
    job_id, _ = create_job(agent_id, file_id, api_key,
                           config_id=config_id, include=include)
    return poll_job(job_id, api_key, include=include)


# --------------------------------------------------------------------------- #
# Output shaping
# --------------------------------------------------------------------------- #

def extract_steps(job):
    """Pull each step's text output out of a completed job into a tidy dict.
    Extraction/classification outputs are JSON strings; parse them when we can."""
    steps = []
    for item in job.get("output", []):
        for part in item.get("content", []):
            if part.get("type") != "output_text":
                continue
            text = part.get("text", "")
            try:
                value = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                value = text
            steps.append({
                "step_index": part.get("step_index"),
                "step_id": part.get("step_id"),
                "output": value,
            })
    steps.sort(key=lambda s: (s["step_index"] is None, s["step_index"]))
    return steps


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
    p.add_argument("--include", choices=["last", "all"], default="last",
                   help="'last' = final step only; 'all' = every step's output")
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
                steps = extract_steps(job)
                results[name] = steps
                cached = (job.get("metadata") or {}).get("cached")
                tag = " (cached)" if cached == "true" else ""
                print(f"  [ok] {name} — {len(steps)} step(s){tag}")
                if args.output:
                    stem = os.path.splitext(name)[0]
                    with open(os.path.join(args.output, f"{stem}.json"), "w") as f:
                        json.dump(steps, f, indent=2, ensure_ascii=False)
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
