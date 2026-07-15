#!/usr/bin/env python3
"""
Upstage Batch Processor v2
===========================
Batch process documents through Upstage Studio Agents (V2 API) or direct V1 APIs.

Primary mode:  V2 Studio Agent — provide your agent ID + a folder of documents.
Fallback mode: V1 direct API — parse, extract, or classify without a Studio workflow.

Features:
  - Concurrent processing with configurable parallelism
  - Exponential backoff retry with Retry-After header support
  - Resume from checkpoint (restart without reprocessing completed docs)
  - Structured logging to file + console
  - Auto-organized results folder
  - Dry-run mode (see what would be processed without calling APIs)
  - HTML summary report
  - Config file support (JSON) with CLI overrides
  - Agent cloning (agent-clone subcommand)
  - Per-step include filters (output:parse, output:extract, etc.)
  - Job statistics (stats subcommand)
  - Per-step error code grouping in failure summaries

Requirements: Python 3.8+, zero third-party dependencies (stdlib only).

Usage:
    # V2 Studio Agent (primary mode)
    python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./my_documents/

    # V2 with config file
    python3 upstage_batch.py agent --config batch_config.json

    # V1 fallback — parse
    python3 upstage_batch.py v1-parse --docs ./my_documents/

    # V1 fallback — extract
    python3 upstage_batch.py v1-extract --docs ./my_documents/ --schema ./schema.json

    # Dry run (any mode)
    python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./my_documents/ --dry-run

    # Resume interrupted run
    python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./my_documents/ --resume

Environment:
    UPSTAGE_API_KEY — required. Your Upstage API key (starts with up_).

Author: Upstage AI — Solutions Engineering
"""

__version__ = "2.5.0"

import json
import urllib.request
import urllib.error
import ssl
import os
import sys
import time
import base64
import mimetypes
import argparse
import logging
import signal
import threading
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# Optional rich support — falls back to ANSI if not installed
try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.columns import Columns
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Optional pikepdf support — used to repair PDFs with corrupted headers
try:
    import pikepdf
    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False


# =============================================================================
# ANSI TERMINAL HELPERS (fallback when rich is not installed)
# =============================================================================

class _Ansi:
    """Minimal ANSI color support for terminals."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"

    @staticmethod
    def supports_color():
        """Check if terminal likely supports ANSI colors."""
        if os.environ.get("NO_COLOR"):
            return False
        if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            return True
        return False

_USE_COLOR = _Ansi.supports_color()

def _c(code, text):
    """Wrap text with ANSI color code if colors are supported."""
    if _USE_COLOR:
        return f"{code}{text}{_Ansi.RESET}"
    return text


def _ansi_progress_bar(done, total, width=30):
    """Render a simple text progress bar using ANSI."""
    if total == 0:
        return ""
    frac = done / total
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    pct = f"{frac * 100:.0f}%"
    if _USE_COLOR:
        return f"{_Ansi.BLUE}{bar}{_Ansi.RESET} {pct}"
    return f"[{bar}] {pct}"


# =============================================================================
# CONSTANTS
# =============================================================================

V2_BASE = "https://api.upstage.ai/v2"
V1_PARSE_URL = "https://api.upstage.ai/v1/document-digitization"
V1_EXTRACT_URL = "https://api.upstage.ai/v1/information-extraction/information-extraction"
V1_CLASSIFY_URL = "https://api.upstage.ai/v1/document-classification/document-classification"

DOC_EXTENSIONS = {
    '.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif',
    '.bmp', '.gif', '.webp', '.heic',
    '.docx', '.pptx', '.xlsx',
}

DEFAULT_RETRY = {
    "max_retries": 3,
    "base_delay": 5,
    "max_delay": 60,
    "backoff_factor": 2,
}

DEFAULT_POLL = {
    "interval": 3,        # seconds between polls
    "max_wait": 2400,     # 40 minutes max per document (client-side give-up).
                          # Upstage's server-side job_timeout_error fires at 1 hour (3600s),
                          # so 2400s is safely under that while covering p99 on large reports.
                          # Jobs still processing when we give up can be recovered by re-running
                          # with --resume — the job_id is retained server-side for 30 days.
}


# =============================================================================
# GRACEFUL SHUTDOWN
# =============================================================================
# Clean exit on Ctrl+C or SIGTERM. Instead of killing the process mid-flight
# (which corrupts the log and leaves the checkpoint out of sync), we set a
# module-level event. The batch loop watches the event and exits cleanly:
#   - stops submitting new work
#   - waits briefly for in-flight work, then cancels
#   - skips the retry pass
#   - flushes the log and writes the final summary
# A second Ctrl+C within ~2s forces a hard exit for impatient users.

_SHUTDOWN_EVENT = threading.Event()
_FORCE_EXIT_DEADLINE = [0.0]  # mutable container for 2nd-Ctrl+C grace period


def _handle_shutdown_signal(signum, frame):
    """Signal handler — sets the shutdown event. Second signal within 2s
    triggers an immediate hard exit."""
    now = time.time()
    if _SHUTDOWN_EVENT.is_set() and now < _FORCE_EXIT_DEADLINE[0]:
        # Second Ctrl+C within grace window — user really means it
        print("\n\nForce exit requested. Terminating immediately "
              "(checkpoint + results already saved for completed docs).\n",
              file=sys.stderr)
        os._exit(130)
    _SHUTDOWN_EVENT.set()
    _FORCE_EXIT_DEADLINE[0] = now + 2.0
    # Direct write — logger may be mid-flush from worker threads
    print(
        "\n\n  Shutdown requested. Finishing in-flight work, then exiting cleanly.\n"
        "  (Press Ctrl+C again within 2s to force-exit immediately.)\n"
        "  Re-run with --resume to pick up where this left off.\n",
        file=sys.stderr,
    )


def install_signal_handlers():
    """Register SIGINT (Ctrl+C) and SIGTERM handlers for graceful shutdown.
    Call once from main() before starting the batch."""
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    # SIGTERM is POSIX-only; on Windows this is a no-op
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        except (ValueError, OSError):
            pass  # not supported in this context (e.g. non-main thread)


def is_shutdown_requested() -> bool:
    """True if a graceful shutdown has been requested via signal."""
    return _SHUTDOWN_EVENT.is_set()


# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging(output_dir: Path, verbose: bool = False) -> logging.Logger:
    """Configure dual logging: file (detailed) + console (summary)."""
    logger = logging.getLogger("upstage_batch")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # File handler — everything
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(output_dir / f"batch_{ts}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console handler — info+
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


# =============================================================================
# HELPERS
# =============================================================================

def get_api_key(cli_key: str = None) -> str:
    """Get API key from --key flag, UPSTAGE_API_KEY env var, or interactive prompt."""
    key = (cli_key or "").strip() or os.environ.get("UPSTAGE_API_KEY", "").strip()
    if not key:
        print("ERROR: No API key provided.")
        print("Provide it via:  --key up_YOUR_KEY")
        print("           or:   export UPSTAGE_API_KEY='up_YOUR_KEY'")
        sys.exit(1)
    return key


def find_documents(path: Path) -> List[Path]:
    """Find all supported document files in a path (file or directory).

    Always returns absolute paths so results are valid regardless of CWD.
    """
    path = Path(os.path.expanduser(str(path))).resolve()
    if path.is_file():
        return [path] if path.suffix.lower() in DOC_EXTENSIONS else []
    elif path.is_dir():
        docs = []
        for ext in DOC_EXTENSIONS:
            docs.extend(path.glob(f"*{ext}"))
        return sorted(set(docs), key=lambda p: p.name.lower())
    return []


def get_mime_type(file_path: Path) -> str:
    """Get MIME type for a file."""
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime or "application/octet-stream"


# Minimum bytes required for a valid PDF header + trailing EOF marker.
# PDFs without at least this much content are almost certainly truncated.
MIN_PDF_SIZE_BYTES = 100


def precheck_file(file_path: Path) -> Optional[str]:
    """Validate a document file before uploading. Returns None if OK, or a
    human-readable reason string if the file should be skipped.

    Catches common pre-upload failure modes that would otherwise waste an
    API call and produce confusing HTTP 415 errors:
      - zero-byte files
      - truncated PDFs (too small to contain a valid header)
      - PDFs without a %PDF- magic-number header
    """
    try:
        size = file_path.stat().st_size
    except OSError as e:
        return f"cannot stat file: {e}"

    if size == 0:
        return "file is 0 bytes (empty/corrupt — likely failed download)"

    # PDF-specific header check
    if file_path.suffix.lower() == ".pdf":
        if size < MIN_PDF_SIZE_BYTES:
            return f"file is only {size} bytes (truncated — needs re-export from source)"
        try:
            with open(file_path, "rb") as f:
                header = f.read(8)
            if not header.startswith(b"%PDF-"):
                return (f"missing PDF magic number (file starts with "
                        f"{header[:8]!r} — not a valid PDF)")
        except OSError as e:
            return f"cannot read file header: {e}"

    return None


def prescreen_documents(documents: List[Path], output_dir: Path,
                        logger: logging.Logger) -> List[Path]:
    """Run precheck_file on every document. Writes skipped files to
    skipped_files.json in the output dir and returns the list of documents
    that passed validation.

    This runs BEFORE any API calls, so corrupt/empty files never consume
    upload quota or produce misleading HTTP 415 errors in the log.
    """
    valid: List[Path] = []
    skipped: List[Dict] = []

    for doc in documents:
        reason = precheck_file(doc)
        if reason is None:
            valid.append(doc)
        else:
            skipped.append({
                "file": doc.name,
                "path": str(doc),
                "reason": reason,
            })
            logger.warning(
                f"  {_c(_Ansi.YELLOW, 'SKIP')}  {doc.name}: {reason}"
            )

    if skipped:
        skip_path = output_dir / "skipped_files.json"
        try:
            with open(skip_path, "w") as f:
                json.dump({
                    "count": len(skipped),
                    "note": ("These files failed pre-upload validation and were "
                             "NOT sent to Upstage. Re-export from source and "
                             "re-run to include them."),
                    "skipped": skipped,
                }, f, indent=2)
            logger.info(
                f"  {_c(_Ansi.YELLOW, 'Note:')} {len(skipped)} file(s) skipped "
                f"due to corruption/empty. See {skip_path.name}"
            )
        except OSError as e:
            logger.error(f"Could not write skipped_files.json: {e}")

    return valid


def make_ssl_context():
    """Create default SSL context."""
    return ssl.create_default_context()


def format_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def format_size(bytes_count: int) -> str:
    """Human-readable file size."""
    if bytes_count < 1024:
        return f"{bytes_count} B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    else:
        return f"{bytes_count / (1024 * 1024):.1f} MB"


# =============================================================================
# RETRY LOGIC
# =============================================================================

def api_request(method: str, url: str, api_key: str,
                headers: Optional[Dict] = None, data: Optional[bytes] = None,
                timeout: int = 300, retry_config: Optional[Dict] = None,
                logger: Optional[logging.Logger] = None) -> Dict:
    """
    Make an HTTP request with exponential backoff retry.
    Respects Retry-After headers on 429s.
    Returns parsed JSON on success, or {"success": False, "error": ...} on failure.
    """
    rc = retry_config or DEFAULT_RETRY
    max_retries = rc["max_retries"]
    delay = rc["base_delay"]
    log = logger or logging.getLogger("upstage_batch")
    ctx = make_ssl_context()

    all_headers = {"Authorization": f"Bearer {api_key}"}
    if headers:
        all_headers.update(headers)

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}

        except urllib.error.HTTPError as e:
            status = e.code
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass

            # Non-retryable client errors (except 429)
            if 400 <= status < 500 and status != 429:
                log.debug(f"HTTP {status} (non-retryable): {err_body}")
                return {"success": False, "error": f"HTTP {status}: {err_body}",
                        "status_code": status}

            if attempt < max_retries - 1:
                retry_after = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                wait = int(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning(f"HTTP {status} — retrying in {wait}s (attempt {attempt + 2}/{max_retries})")
                time.sleep(wait)
                delay = min(delay * rc["backoff_factor"], rc["max_delay"])
            else:
                return {"success": False,
                        "error": f"HTTP {status} after {max_retries} attempts: {err_body}",
                        "status_code": status}

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < max_retries - 1:
                log.warning(f"Network error: {e} — retrying in {delay}s (attempt {attempt + 2}/{max_retries})")
                time.sleep(delay)
                delay = min(delay * rc["backoff_factor"], rc["max_delay"])
            else:
                return {"success": False,
                        "error": f"Network error after {max_retries} attempts: {e}"}

    return {"success": False, "error": "All retries exhausted"}


# =============================================================================
# CHECKPOINT / RESUME
# =============================================================================

class Checkpoint:
    """Simple file-based checkpoint for resume support."""

    def __init__(self, checkpoint_path: Path):
        self.path = checkpoint_path
        self.completed = set()  # filenames that completed successfully
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.completed = set(data.get("completed", []))
            except (json.JSONDecodeError, KeyError):
                self.completed = set()

    def save(self):
        self.path.write_text(json.dumps({
            "completed": sorted(self.completed),
            "last_updated": datetime.now().isoformat(),
        }, indent=2))

    def mark_done(self, filename: str):
        self.completed.add(filename)
        self.save()

    def is_done(self, filename: str) -> bool:
        return filename in self.completed

    def remaining(self, all_docs: List[Path]) -> List[Path]:
        return [d for d in all_docs if d.name not in self.completed]


# =============================================================================
# V2 STUDIO AGENT MODE
# =============================================================================

def repair_pdf_if_needed(file_path: Path,
                         logger: logging.Logger) -> Tuple[Path, bool]:
    """Check if a PDF has a corrupted header and repair it if possible.

    Returns (path_to_use, was_repaired). If repaired, path_to_use is a temp
    file that the caller should delete after upload.
    """
    if file_path.suffix.lower() != ".pdf":
        return file_path, False

    with open(file_path, "rb") as f:
        header = f.read(5)

    if header == b"%PDF-":
        return file_path, False

    # Bad header detected
    if not HAS_PIKEPDF:
        logger.warning(f"  {file_path.name}: PDF header is corrupted (got {header!r}, expected b'%PDF-'). "
                       f"Install pikepdf to enable auto-repair: pip install pikepdf")
        return file_path, False

    logger.info(f"  {file_path.name}: PDF header corrupted — repairing with pikepdf...")
    try:
        repaired_path = file_path.parent / f".repaired_{file_path.name}"
        with pikepdf.open(file_path) as doc:
            doc.save(repaired_path)
        logger.info(f"  {file_path.name}: PDF headers repaired, uploading repaired copy")
        return repaired_path, True
    except Exception as e:
        logger.error(f"  {file_path.name}: PDF repair failed: {e}")
        return file_path, False


def v2_upload_file(file_path: Path, api_key: str,
                   logger: logging.Logger) -> Optional[str]:
    """Upload a file to V2 and return the file_id."""
    # Auto-repair corrupted PDF headers before upload
    upload_path, was_repaired = repair_pdf_if_needed(file_path, logger)

    boundary = f"----Boundary{int(time.time() * 1000)}"
    filename = file_path.name  # Always use original filename for the API
    mime = get_mime_type(file_path)

    try:
        with open(upload_path, "rb") as f:
            file_bytes = f.read()

        lines = []
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {mime}".encode())
        lines.append(b"")
        lines.append(file_bytes)
        lines.append(f"--{boundary}".encode())
        lines.append(b'Content-Disposition: form-data; name="purpose"')
        lines.append(b"")
        lines.append(b"user_data")
        lines.append(f"--{boundary}--".encode())
        lines.append(b"")

        body = b"\r\n".join(lines)
        content_type = f"multipart/form-data; boundary={boundary}"

        result = api_request(
            "POST", f"{V2_BASE}/files", api_key,
            headers={"Content-Type": content_type},
            data=body, timeout=120, logger=logger,
        )

        if isinstance(result, dict) and result.get("id"):
            file_id = result["id"]
            logger.debug(f"Uploaded {filename} -> {file_id}")
            # Wait until page-image conversion finishes before returning. Creating
            # a job against a file that's still PROCESSING returns 409 (non-
            # retryable here), so a large/slow-converting doc would otherwise fail
            # its first pass and only recover on the auto-retry or --resume.
            for _ in range(60):  # ~2 min ceiling at 2s intervals
                if is_shutdown_requested():
                    break
                info = api_request(
                    "GET", f"{V2_BASE}/files/{file_id}?view=status",
                    api_key, timeout=30, logger=logger,
                )
                status = (str(info.get("status", "")).upper()
                          if isinstance(info, dict) else "")
                if status in ("UPLOADED", "READY"):
                    break
                if status == "FAILED":
                    logger.error(f"File conversion failed for {filename}")
                    return None
                time.sleep(2)
            return file_id

        err = result.get("error", "Unknown upload error")
        logger.error(f"Upload failed for {filename}: {err}")
        return None
    finally:
        # Clean up repaired temp file
        if was_repaired and upload_path.exists():
            upload_path.unlink()


def v2_create_job(file_id: str, agent_id: str, api_key: str,
                  config_id: Optional[str] = None,
                  include: List[str] = None,
                  logger: logging.Logger = None) -> Optional[str]:
    """Create a V2 agent job and return the job_id."""
    payload = {
        "model": agent_id,
        "input": [{"role": "user", "content": [{"type": "input_file", "file_id": file_id}]}],
        "include": include or ["last"],
    }
    if config_id:
        payload["config_id"] = config_id

    data = json.dumps(payload).encode("utf-8")

    result = api_request(
        "POST", f"{V2_BASE}/responses", api_key,
        headers={"Content-Type": "application/json"},
        data=data, timeout=60, logger=logger,
    )

    if isinstance(result, dict) and result.get("id"):
        logger.debug(f"Created job {result['id']} for file {file_id}")
        return result["id"]

    err = result.get("error", "Unknown job creation error")
    logger.error(f"Job creation failed for file {file_id}: {err}")
    return None


def v2_poll_job(job_id: str, api_key: str, include: List[str] = None,
                poll_config: Optional[Dict] = None,
                logger: logging.Logger = None) -> Dict:
    """Poll a V2 job until completed or failed. Returns the final response."""
    pc = poll_config or DEFAULT_POLL
    interval = pc["interval"]
    max_wait = pc["max_wait"]
    start = time.time()

    while time.time() - start < max_wait:
        # Graceful-shutdown check — exit the poll loop immediately if user
        # requested Ctrl+C. The job keeps running server-side and can be
        # recovered with --resume.
        if is_shutdown_requested():
            return {"success": False,
                    "error": f"Shutdown requested — job {job_id} left running "
                             f"server-side. Re-run with --resume to recover.",
                    "error_code": "shutdown_requested",
                    "job_id": job_id,
                    "recoverable": True}

        inc = include or ["last"]
        qs = "&".join(f"include[]={v}" for v in inc)
        result = api_request(
            "GET", f"{V2_BASE}/responses/{job_id}?{qs}",
            api_key, timeout=30, logger=logger,
        )

        if not isinstance(result, dict):
            time.sleep(interval)
            continue

        # Check for API error
        if result.get("success") is False:
            return result

        status = result.get("status", "")
        if status == "completed":
            return result
        elif status == "failed":
            error_obj = result.get("error", {})
            error_code = "unknown_error"
            error_msg = str(error_obj)
            if isinstance(error_obj, dict):
                error_code = error_obj.get("code", "unknown_error")
                error_msg = error_obj.get("message", str(error_obj))
            logger.error(f"ERROR [{error_code}] Job {job_id}: {error_msg}")
            return {"success": False, "error": f"Job failed: {error_msg}",
                    "error_code": error_code, "raw": result}

        logger.debug(f"Job {job_id}: {status} (elapsed {time.time() - start:.0f}s)")
        time.sleep(interval)

    # Client-side poll deadline reached. The job is almost certainly still running
    # on Upstage's side (server-side job_timeout_error doesn't fire until 3600s).
    # Re-running with --resume will pick the job up by job_id and fetch final results
    # without re-uploading or re-billing.
    logger.warning(
        f"Client poll deadline ({max_wait}s) reached for job {job_id} — "
        f"job is likely still processing server-side. Re-run with --resume to "
        f"collect final results (job results retained 30 days)."
    )
    return {"success": False,
            "error": f"Client poll timeout after {max_wait}s — job {job_id} "
                     f"still processing server-side. Re-run with --resume to recover.",
            "error_code": "client_poll_timeout",
            "job_id": job_id,
            "recoverable": True}


def v2_process_one(file_path: Path, agent_id: str, api_key: str,
                   config_id: Optional[str] = None,
                   include: List[str] = None,
                   poll_config: Optional[Dict] = None,
                   logger: logging.Logger = None) -> Dict:
    """Process a single document through a V2 Studio Agent: upload -> create job -> poll."""
    start_time = time.time()
    result = {
        "document": file_path.name,
        "file_path": str(file_path),
        "file_size": file_path.stat().st_size,
        "mode": "v2-agent",
        "agent_id": agent_id,
        "success": False,
        "timestamp": datetime.now().isoformat(),
    }

    # Step 1: Upload
    logger.debug(f"  {file_path.name}: uploading...")
    file_id = v2_upload_file(file_path, api_key, logger)
    if not file_id:
        result["error"] = "File upload failed"
        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result
    result["file_id"] = file_id
    logger.debug(f"  {file_path.name}: uploaded ({file_id})")

    # Step 2: Create job
    job_id = v2_create_job(file_id, agent_id, api_key, config_id, include, logger)
    if not job_id:
        result["error"] = "Job creation failed"
        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result
    result["job_id"] = job_id
    logger.debug(f"  {file_path.name}: processing ({job_id})")

    # Step 3: Poll
    job_result = v2_poll_job(job_id, api_key, include, poll_config, logger)

    if job_result.get("success") is False:
        result["error"] = job_result.get("error", "Polling failed")
        result["error_code"] = job_result.get("error_code", "unknown_error")
        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result

    # Parse output
    result["success"] = True
    result["status"] = job_result.get("status")
    result["usage"] = job_result.get("usage", {})

    # Extract the output content
    output = job_result.get("output", [])
    if output:
        extracted = {}
        for step in output:
            step_model = step.get("model", "unknown")
            contents = step.get("content", [])
            for content in contents:
                text = content.get("text", "")
                try:
                    parsed = json.loads(text) if text else text
                except (json.JSONDecodeError, TypeError):
                    parsed = text
                extracted[step_model] = {
                    "data": parsed,
                    "additional_values": content.get("additional_values"),
                }
        result["extracted"] = extracted

        # Also set a convenience "final_output" from the last step
        if output:
            last_step = output[-1]
            last_content = last_step.get("content", [{}])
            if last_content:
                text = last_content[0].get("text", "")
                try:
                    result["final_output"] = json.loads(text) if text else text
                except (json.JSONDecodeError, TypeError):
                    result["final_output"] = text
    else:
        # Try output_text shortcut
        ot = job_result.get("output_text")
        if ot:
            try:
                result["final_output"] = json.loads(ot) if isinstance(ot, str) else ot
            except (json.JSONDecodeError, TypeError):
                result["final_output"] = ot

    result["duration_seconds"] = round(time.time() - start_time, 2)
    return result


def v2_clone_agent(source_agent_id: str, name: str, api_key: str,
                   with_jobs: bool = False, config_id: Optional[str] = None,
                   source: Optional[str] = None, dry_run: bool = False,
                   logger: logging.Logger = None) -> Dict:
    """Clone an existing agent via POST /v2/agents with clone payload."""
    payload = {
        "name": name,
        "clone": {
            "agent_id": source_agent_id,
            "with_jobs": with_jobs,
        }
    }
    if config_id:
        payload["clone"]["config_id"] = config_id
    if source:
        payload["clone"]["source"] = source

    if dry_run:
        if logger:
            logger.info("DRY RUN — would POST /v2/agents with payload:")
            logger.info(json.dumps(payload, indent=2))
        return {"dry_run": True, "payload": payload}

    data = json.dumps(payload).encode("utf-8")
    result = api_request(
        "POST", f"{V2_BASE}/agents", api_key,
        headers={"Content-Type": "application/json"},
        data=data, timeout=60, logger=logger,
    )

    if isinstance(result, dict) and result.get("id"):
        return {"success": True, "agent_id": result["id"],
                "source_agent_id": result.get("source_agent_id"),
                "name": result.get("name"), "raw": result}

    err = result.get("error", "Unknown clone error")
    return {"success": False, "error": str(err)}


def v2_get_stats(api_key: str, agent_id: Optional[str] = None,
                 config_id: Optional[str] = None, source: Optional[str] = None,
                 since: Optional[str] = None, is_custom: Optional[bool] = None,
                 logger: logging.Logger = None) -> Dict:
    """Fetch job statistics from GET /v2/stats/jobs."""
    params = []
    if agent_id:
        params.append(f"agent_id={agent_id}")
    if config_id:
        params.append(f"config_id={config_id}")
    if source:
        params.append(f"source={source}")
    if since:
        params.append(f"since={since}")
    if is_custom is not None:
        params.append(f"is_custom={'true' if is_custom else 'false'}")

    qs = ("?" + "&".join(params)) if params else ""
    result = api_request(
        "GET", f"{V2_BASE}/stats/jobs{qs}", api_key,
        timeout=30, logger=logger,
    )
    return result


# =============================================================================
# V1 FALLBACK MODES
# =============================================================================

def v1_parse_one(file_path: Path, api_key: str, options: Dict,
                 logger: logging.Logger) -> Dict:
    """Parse a single document via V1 Document Parse API."""
    start_time = time.time()
    result = {
        "document": file_path.name,
        "file_path": str(file_path),
        "file_size": file_path.stat().st_size,
        "mode": "v1-parse",
        "success": False,
        "timestamp": datetime.now().isoformat(),
    }

    # Auto-repair corrupted PDF headers before upload
    upload_path, was_repaired = repair_pdf_if_needed(file_path, logger)

    # Build multipart form data
    boundary = f"----Boundary{int(time.time() * 1000)}"
    mime = get_mime_type(file_path)
    with open(upload_path, "rb") as f:
        file_bytes = f.read()

    # Clean up repaired temp file now that bytes are in memory
    if was_repaired and upload_path.exists():
        upload_path.unlink()

    lines = []
    # File field
    lines.append(f"--{boundary}".encode())
    lines.append(f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"'.encode())
    lines.append(f"Content-Type: {mime}".encode())
    lines.append(b"")
    lines.append(file_bytes)

    # Model param
    model = options.get("model", "document-parse")
    for name, value in [("model", model), ("ocr", options.get("ocr", "auto"))]:
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode())

    if options.get("coordinates", True):
        lines.append(f"--{boundary}".encode())
        lines.append(b'Content-Disposition: form-data; name="coordinates"')
        lines.append(b"")
        lines.append(b"true")

    if options.get("output_formats"):
        lines.append(f"--{boundary}".encode())
        lines.append(b'Content-Disposition: form-data; name="output_formats"')
        lines.append(b"")
        lines.append(json.dumps(options["output_formats"]).encode())

    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"

    api_result = api_request(
        "POST", V1_PARSE_URL, api_key,
        headers={"Content-Type": content_type},
        data=body, timeout=180, logger=logger,
    )

    if isinstance(api_result, dict) and api_result.get("success") is False:
        result["error"] = api_result["error"]
    else:
        result["success"] = True
        result["final_output"] = api_result
        result["usage"] = api_result.get("usage", {})

    result["duration_seconds"] = round(time.time() - start_time, 2)
    return result


def v1_extract_one(file_path: Path, schema: Dict, api_key: str,
                   model: str, logger: logging.Logger) -> Dict:
    """Extract from a single document via V1 IE API."""
    start_time = time.time()
    result = {
        "document": file_path.name,
        "file_path": str(file_path),
        "file_size": file_path.stat().st_size,
        "mode": "v1-extract",
        "success": False,
        "timestamp": datetime.now().isoformat(),
    }

    # Auto-repair corrupted PDF headers before upload
    upload_path, was_repaired = repair_pdf_if_needed(file_path, logger)

    try:
        with open(upload_path, "rb") as f:
            doc_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as e:
        result["error"] = f"File read error: {e}"
        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result
    finally:
        if was_repaired and upload_path.exists():
            upload_path.unlink()

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [{"type": "image_url",
                          "image_url": {"url": f"data:application/octet-stream;base64,{doc_b64}"}}],
        }],
        "response_format": schema,
    }
    data = json.dumps(payload).encode("utf-8")

    api_result = api_request(
        "POST", V1_EXTRACT_URL, api_key,
        headers={"Content-Type": "application/json"},
        data=data, timeout=300, logger=logger,
    )

    if isinstance(api_result, dict) and api_result.get("success") is False:
        result["error"] = api_result["error"]
    else:
        result["success"] = True
        result["usage"] = api_result.get("usage", {})
        if "choices" in api_result and api_result["choices"]:
            content = api_result["choices"][0].get("message", {}).get("content", "{}")
            try:
                result["final_output"] = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                result["final_output"] = content
        result["raw_response"] = api_result

    result["duration_seconds"] = round(time.time() - start_time, 2)
    return result


def v1_classify_one(file_path: Path, schema: Dict, api_key: str,
                    logger: logging.Logger) -> Dict:
    """Classify a single document via V1 Classification API."""
    start_time = time.time()
    result = {
        "document": file_path.name,
        "file_path": str(file_path),
        "file_size": file_path.stat().st_size,
        "mode": "v1-classify",
        "success": False,
        "timestamp": datetime.now().isoformat(),
    }

    # Auto-repair corrupted PDF headers before upload
    upload_path, was_repaired = repair_pdf_if_needed(file_path, logger)

    try:
        with open(upload_path, "rb") as f:
            doc_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as e:
        result["error"] = f"File read error: {e}"
        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result
    finally:
        if was_repaired and upload_path.exists():
            upload_path.unlink()

    payload = {
        "model": "document-classify",
        "messages": [{
            "role": "user",
            "content": [{"type": "image_url",
                          "image_url": {"url": f"data:application/octet-stream;base64,{doc_b64}"}}],
        }],
        "response_format": schema,
    }
    data = json.dumps(payload).encode("utf-8")

    api_result = api_request(
        "POST", V1_CLASSIFY_URL, api_key,
        headers={"Content-Type": "application/json"},
        data=data, timeout=300, logger=logger,
    )

    if isinstance(api_result, dict) and api_result.get("success") is False:
        result["error"] = api_result["error"]
    else:
        result["success"] = True
        result["usage"] = api_result.get("usage", {})
        if "choices" in api_result and api_result["choices"]:
            content = api_result["choices"][0].get("message", {}).get("content", "{}")
            try:
                result["final_output"] = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                result["final_output"] = content

    result["duration_seconds"] = round(time.time() - start_time, 2)
    return result


# =============================================================================
# BATCH ORCHESTRATOR
# =============================================================================

def _progress_ticker(total: int, completed_count, in_flight_count,
                     batch_start: float, logger: logging.Logger,
                     stop_event, success_count=None, fail_count=None):
    """Background thread that prints periodic progress updates."""
    import threading
    interval = 10  # seconds between ticker updates
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break
        done = completed_count[0]
        flying = in_flight_count[0]
        ok = success_count[0] if success_count else done
        fails = fail_count[0] if fail_count else 0
        elapsed = time.time() - batch_start
        if done > 0:
            throughput = done / elapsed
            remaining_secs = (total - done) / throughput
            eta = format_duration(remaining_secs)
        else:
            eta = "calculating..."

        bar = _ansi_progress_bar(done, total)
        ok_str = _c(_Ansi.GREEN, f"{ok} ok")
        fail_str = _c(_Ansi.RED, f"{fails} fail") if fails > 0 else f"{fails} fail"
        fly_str = _c(_Ansi.CYAN, f"{flying} active")

        logger.info(
            f"  {bar}  {done}/{total}  |  {ok_str}  {fail_str}  |  "
            f"{fly_str}  |  ~{eta} remaining"
        )


def _save_result_immediately(r: Dict, output_dir: Path, logger: logging.Logger):
    """Write a single result JSON to disk as soon as it completes."""
    try:
        results_dir = output_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        doc_name = Path(r.get("document", "unknown")).stem
        result_path = results_dir / f"{doc_name}.json"
        with open(result_path, "w") as f:
            json.dump(r, f, indent=2, default=str)
    except Exception as e:
        logger.debug(f"Could not save intermediate result for {r.get('document')}: {e}")


def run_batch(process_fn, documents: List[Path], parallel: int,
              checkpoint: Optional[Checkpoint], logger: logging.Logger,
              output_dir: Optional[Path] = None,
              **kwargs) -> List[Dict]:
    """
    Generic batch runner. Calls process_fn(doc, **kwargs) for each document.
    Handles parallelism, progress reporting, checkpoint, and retry of failures.
    Results are saved to disk immediately as each document completes.
    """
    import threading

    total = len(documents)
    results = []
    failed = []

    if total == 0:
        logger.info("No documents to process.")
        return results

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Processing {total} document(s)  |  Workers: {parallel}")
    logger.info(f"{'=' * 60}\n")

    batch_start = time.time()

    success_count = [0]
    fail_count = [0]

    if parallel > 1 and total > 1:
        # Shared counters for progress ticker (using lists for mutability in threads)
        completed_count = [0]
        in_flight_count = [0]
        stop_event = threading.Event()

        # Start progress ticker thread
        ticker = threading.Thread(
            target=_progress_ticker,
            args=(total, completed_count, in_flight_count, batch_start, logger, stop_event,
                  success_count, fail_count),
            daemon=True,
        )
        ticker.start()

        # Submit jobs one at a time so we can log each submission
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            future_map = {}
            for idx, doc in enumerate(documents, 1):
                # If shutdown requested mid-submission, stop queuing new work.
                # Already-submitted futures still finish (via shutdown-aware poll).
                if is_shutdown_requested():
                    logger.warning(
                        f"  Shutdown mid-submission — {total - idx + 1} "
                        f"document(s) not submitted. Re-run with --resume."
                    )
                    break
                future = pool.submit(process_fn, doc, **kwargs)
                future_map[future] = (idx, doc)
                in_flight_count[0] += 1
                logger.info(f"[{idx}/{total}] Submitted  {doc.name}  ({format_size(doc.stat().st_size)})")

            for future in concurrent.futures.as_completed(future_map):
                submit_idx, doc = future_map[future]
                try:
                    r = future.result()
                except Exception as e:
                    r = {"document": doc.name, "success": False, "error": str(e),
                         "duration_seconds": 0}

                results.append(r)
                completed_count[0] += 1
                in_flight_count[0] = max(0, in_flight_count[0] - 1)
                done = completed_count[0]
                dur = r.get("duration_seconds", 0)

                # Save result to disk immediately
                if output_dir:
                    _save_result_immediately(r, output_dir, logger)

                if r["success"]:
                    success_count[0] += 1
                    tag = _c(_Ansi.GREEN, "OK")
                    logger.info(f"[{done}/{total}] {tag}  {doc.name}  ({format_duration(dur)})")
                else:
                    fail_count[0] += 1
                    tag = _c(_Ansi.RED, "FAIL")
                    failed.append(doc)
                    err_short = r.get("error", "unknown error")[:120]
                    err_code = r.get("error_code", "")
                    code_label = f" [{err_code}]" if err_code else ""
                    logger.info(f"[{done}/{total}] {tag}  {doc.name}  ({format_duration(dur)})")
                    logger.info(f"      {_c(_Ansi.RED, 'Error' + code_label + ':')} {err_short}")
                    logger.info(f"      {_c(_Ansi.YELLOW, 'Will retry')} after all documents complete (1 auto-retry)")

                if r["success"] and checkpoint:
                    checkpoint.mark_done(doc.name)

        stop_event.set()
        ticker.join(timeout=2)
    else:
        for i, doc in enumerate(documents, 1):
            bar = _ansi_progress_bar(i - 1, total, width=20)
            logger.info(f"{bar}  [{i}/{total}] Processing {doc.name}...")
            try:
                r = process_fn(doc, **kwargs)
            except Exception as e:
                r = {"document": doc.name, "success": False, "error": str(e),
                     "duration_seconds": 0}

            results.append(r)
            dur = r.get("duration_seconds", 0)

            # Save result to disk immediately
            if output_dir:
                _save_result_immediately(r, output_dir, logger)

            if r["success"]:
                success_count[0] += 1
                tag = _c(_Ansi.GREEN, "OK")
                logger.info(f"[{i}/{total}] {tag}  {doc.name}  ({format_duration(dur)})")
            else:
                fail_count[0] += 1
                tag = _c(_Ansi.RED, "FAIL")
                failed.append(doc)
                err_short = r.get("error", "unknown error")[:120]
                err_code = r.get("error_code", "")
                code_label = f" [{err_code}]" if err_code else ""
                logger.info(f"[{i}/{total}] {tag}  {doc.name}  ({format_duration(dur)})")
                logger.info(f"      {_c(_Ansi.RED, 'Error' + code_label + ':')} {err_short}")
                logger.info(f"      {_c(_Ansi.YELLOW, 'Will retry')} after all documents complete (1 auto-retry)")

            if r["success"] and checkpoint:
                checkpoint.mark_done(doc.name)

    # Retry failures once after a cooldown — unless shutdown was requested,
    # in which case we skip retries entirely. Retries should happen on an
    # explicit --resume run, not during a user-interrupted session.
    if failed and is_shutdown_requested():
        logger.warning(
            f"\n  Shutdown requested — skipping auto-retry of "
            f"{len(failed)} failed document(s). Re-run with --resume."
        )
    elif failed:
        logger.info(f"\n--- Retrying {len(failed)} failed document(s) after 10s cooldown ---")
        time.sleep(10)

        for doc in failed:
            # Also bail out of the retry loop if shutdown fires mid-retry
            if is_shutdown_requested():
                logger.warning(
                    f"  Shutdown mid-retry — remaining failures deferred. "
                    f"Re-run with --resume."
                )
                break
            logger.info(f"  Retrying {doc.name}...")
            try:
                r = process_fn(doc, **kwargs)
            except Exception as e:
                r = {"document": doc.name, "success": False, "error": str(e),
                     "duration_seconds": 0}

            # Replace the failed result and save updated file
            for idx, existing in enumerate(results):
                if existing["document"] == doc.name and not existing["success"]:
                    results[idx] = r
                    break
            if output_dir:
                _save_result_immediately(r, output_dir, logger)

            dur = format_duration(r.get('duration_seconds', 0))
            if r["success"]:
                logger.info(f"  {_c(_Ansi.GREEN, 'OK')}  {doc.name}  ({dur})")
            else:
                err_short = r.get("error", "unknown error")[:120]
                err_code = r.get("error_code", "")
                code_label = f" [{err_code}]" if err_code else ""
                logger.info(f"  {_c(_Ansi.RED, 'STILL FAILING')}  {doc.name}  ({dur})")
                logger.info(f"      {_c(_Ansi.RED, 'Error' + code_label + ':')} {err_short}")
                logger.info(f"      {_c(_Ansi.DIM, 'No more auto-retries. Use --resume to retry later.')}")

            if r["success"] and checkpoint:
                checkpoint.mark_done(doc.name)

    return results


# =============================================================================
# REPORTING
# =============================================================================

def generate_summary(results: List[Dict], mode: str, start_time: float,
                     output_dir: Path, logger: logging.Logger) -> Dict:
    """Generate summary statistics and save results."""
    total = len(results)
    successful = sum(1 for r in results if r.get("success"))
    failed = total - successful
    total_time = time.time() - start_time

    # Count documents with recoverable errors (client-side timeouts or
    # shutdown) separately — these are NOT real failures, they're just
    # incomplete and will finish on the next --resume run.
    recoverable = sum(1 for r in results
                      if not r.get("success") and r.get("recoverable"))

    summary = {
        "mode": mode,
        "total_documents": total,
        "successful": successful,
        "failed": failed,
        "recoverable_incomplete": recoverable,
        "success_rate": round(successful / total * 100, 1) if total else 0,
        "total_duration": round(total_time, 2),
        "avg_duration": round(total_time / total, 2) if total else 0,
        "timestamp": datetime.now().isoformat(),
        "shutdown_requested": is_shutdown_requested(),
    }

    # Group errors by error_code
    error_codes = {}
    for r in results:
        if not r.get("success") and r.get("error_code"):
            code = r.get("error_code")
            if code not in error_codes:
                error_codes[code] = []
            error_codes[code].append(r.get("document", "unknown"))
    if error_codes:
        summary["error_groups"] = error_codes

    # Save individual results as JSON
    results_dir = output_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    for r in results:
        doc_name = Path(r.get("document", "unknown")).stem
        result_path = results_dir / f"{doc_name}.json"
        with open(result_path, "w") as f:
            json.dump(r, f, indent=2, default=str)

    # Save summary
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump({**summary, "results": results}, f, indent=2, default=str)

    # Print summary — use rich if available, else ANSI
    if HAS_RICH:
        _print_rich_summary(summary, results, results_dir, summary_path, mode,
                            total_time, successful, failed, total)
    else:
        _print_ansi_summary(summary, results, results_dir, summary_path, mode,
                            total_time, successful, failed, total, logger)

    return summary


def _print_ansi_summary(summary, results, results_dir, summary_path, mode,
                        total_time, successful, failed, total, logger):
    """Print a nicely formatted ANSI terminal summary."""
    sr = summary["success_rate"]

    # Header
    logger.info("")
    logger.info(_c(_Ansi.BOLD, "  ╔══════════════════════════════════════════════════════╗"))
    logger.info(_c(_Ansi.BOLD, "  ║           BATCH PROCESSING COMPLETE                  ║"))
    logger.info(_c(_Ansi.BOLD, "  ╚══════════════════════════════════════════════════════╝"))
    logger.info("")

    # Stats
    if sr >= 90:
        rate_str = _c(_Ansi.GREEN + _Ansi.BOLD, f"{sr}%")
    elif sr >= 70:
        rate_str = _c(_Ansi.YELLOW + _Ansi.BOLD, f"{sr}%")
    else:
        rate_str = _c(_Ansi.RED + _Ansi.BOLD, f"{sr}%")

    logger.info(f"  {_c(_Ansi.DIM, 'Mode:')}         {mode}")
    logger.info(f"  {_c(_Ansi.DIM, 'Success rate:')}  {rate_str}  ({_c(_Ansi.GREEN, str(successful))} passed, {_c(_Ansi.RED, str(failed)) if failed else '0'} failed, {total} total)")
    logger.info(f"  {_c(_Ansi.DIM, 'Total time:')}    {format_duration(total_time)}")
    logger.info(f"  {_c(_Ansi.DIM, 'Avg per doc:')}   {format_duration(summary['avg_duration'])}")
    logger.info(f"  {_c(_Ansi.DIM, 'Results:')}       {results_dir}/")
    logger.info(f"  {_c(_Ansi.DIM, 'Summary:')}       {summary_path}")

    # Progress bar final state
    bar = _ansi_progress_bar(successful, total, width=40)
    logger.info(f"\n  {bar}")

    # Failed documents
    if failed > 0:
        logger.info(f"\n  {_c(_Ansi.RED + _Ansi.BOLD, f'{failed} failed document(s):')}")
        for r in results:
            if not r.get("success"):
                logger.info(f"    {_c(_Ansi.RED, '✗')} {r['document']}")
                logger.info(f"      {_c(_Ansi.DIM, r.get('error', 'unknown')[:100])}")

        logger.info(f"\n  {_c(_Ansi.YELLOW, 'Tip:')} Run the same command with {_c(_Ansi.BOLD, '--resume')} to retry only failed docs.")

    # Error groups by error_code
    if summary.get("error_groups"):
        logger.info(f"\n  {_c(_Ansi.YELLOW + _Ansi.BOLD, 'Errors by code:')}")
        for code, docs in summary["error_groups"].items():
            logger.info(f"    {_c(_Ansi.YELLOW, code)}: {len(docs)} document(s)")
            for doc in docs[:5]:
                logger.info(f"      • {doc}")
            if len(docs) > 5:
                logger.info(f"      • ... and {len(docs) - 5} more")

    logger.info("")


def _print_rich_summary(summary, results, results_dir, summary_path, mode,
                        total_time, successful, failed, total):
    """Print a rich-formatted summary panel (only called when rich is available)."""
    console = Console()
    sr = summary["success_rate"]

    # Build stats table
    stats = Table(show_header=False, box=None, padding=(0, 3))
    stats.add_column("label", style="dim")
    stats.add_column("value")

    rate_style = "bold green" if sr >= 90 else "bold yellow" if sr >= 70 else "bold red"
    stats.add_row("Mode", mode)
    stats.add_row("Success Rate", Text(f"{sr}%", style=rate_style))
    stats.add_row("Documents", f"[green]{successful}[/green] passed, [red]{failed}[/red] failed, {total} total")
    stats.add_row("Total Time", format_duration(total_time))
    stats.add_row("Avg / Doc", format_duration(summary["avg_duration"]))
    stats.add_row("Results", str(results_dir) + "/")
    stats.add_row("Summary", str(summary_path))

    title_style = "bold green" if sr >= 90 else "bold yellow" if sr >= 70 else "bold red"
    console.print()
    console.print(Panel(stats, title="[bold]Batch Complete[/bold]", border_style=title_style, padding=(1, 2)))

    # Failed documents table
    if failed > 0:
        fail_table = Table(title=f"{failed} Failed Document(s)", show_lines=False, border_style="red")
        fail_table.add_column("Document", style="white", max_width=50)
        fail_table.add_column("Error", style="dim", max_width=60)

        for r in results:
            if not r.get("success"):
                fail_table.add_row(r["document"], r.get("error", "unknown")[:80])

        console.print(fail_table)
        console.print(f"\n  [yellow]Tip:[/yellow] Run the same command with [bold]--resume[/bold] to retry only failed docs.\n")

        # Error groups by error_code
        if summary.get("error_groups"):
            error_table = Table(title="Errors by Code", show_lines=False, border_style="yellow")
            error_table.add_column("Error Code", style="yellow")
            error_table.add_column("Count", style="white")
            error_table.add_column("Documents", style="dim")

            for code, docs in summary["error_groups"].items():
                doc_list = ", ".join(docs[:3])
                if len(docs) > 3:
                    doc_list += f", ... +{len(docs) - 3}"
                error_table.add_row(code, str(len(docs)), doc_list)

            console.print(error_table)
    else:
        console.print()


def generate_html_report(results: List[Dict], summary: Dict,
                         output_dir: Path) -> Path:
    """Generate an HTML summary report using Upstage brand guidelines."""
    report_path = output_dir / "report.html"

    # Build document rows
    doc_rows = ""
    for r in sorted(results, key=lambda x: x.get("document", "")):
        ok = r.get("success", False)
        status_icon = "&#10003;" if ok else "&#10007;"
        status_class = "status-ok" if ok else "status-fail"
        dur = r.get("duration_seconds", 0)
        size = format_size(r.get("file_size", 0))
        error = r.get("error", "-") if not ok else "-"
        doc_rows += f"""<tr>
            <td>{r.get('document', '?')}</td>
            <td>{size}</td>
            <td class="{status_class}">{status_icon}</td>
            <td>{format_duration(dur)}</td>
            <td class="error-cell">{error[:100]}</td>
        </tr>\n"""

    sr = summary["success_rate"]
    sr_class = "val-success" if sr >= 90 else "val-warning" if sr >= 70 else "val-error"

    # Mode display name
    mode_display = summary['mode'].replace("v2-agent", "Upstage Studio").replace("v1-", "V1 ")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Upstage Batch Report</title>
<style>
/* System font stack only — the report opens fully offline and never fetches
   third-party assets (matches the toolkit's no-phone-home posture). */

:root {{
  --bg-dark: #191722;
  --slate: #434C60;
  --gray: #979CAE;
  --purple: #3A37BD;
  --light-purple: #C2C6FF;
  --pink: #F3BCFC;
  --card-border: rgba(255,255,255,0.12);
  --card-bg: rgba(255,255,255,0.04);
  --row-hover: rgba(255,255,255,0.06);
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg-dark);
  color: #fff;
  padding: 40px 20px;
  min-height: 100vh;
  /* Diagonal line background treatment */
  background-image:
    repeating-linear-gradient(
      -45deg,
      transparent,
      transparent 40px,
      rgba(255,255,255,0.025) 40px,
      rgba(255,255,255,0.025) 41px
    );
}}

.container {{ max-width: 960px; margin: 0 auto; }}

/* Header */
.header {{
  margin-bottom: 32px;
}}
.header h1 {{
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.5px;
  margin-bottom: 6px;
}}
.header h1 .kw {{
  background: linear-gradient(90deg, #3A37BD, #C2C6FF, #F3BCFC);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.subtitle {{
  color: var(--gray);
  font-size: 13px;
  font-weight: 400;
}}

/* Cards */
.card {{
  background: var(--card-bg);
  border: 1px solid var(--card-border);
  border-radius: 16px;
  padding: 28px;
  margin-bottom: 16px;
}}

/* Stats grid */
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 12px;
}}
.stat {{
  text-align: center;
  padding: 20px 12px;
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 12px;
}}
.stat-val {{
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -1px;
}}
.stat-lbl {{
  font-size: 10px;
  color: var(--gray);
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-top: 4px;
}}
.val-success {{ color: #C2C6FF; }}
.val-warning {{ color: #F3BCFC; }}
.val-error {{ color: #F3BCFC; }}
.val-accent {{
  background: linear-gradient(90deg, #3A37BD, #C2C6FF, #F3BCFC);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.val-neutral {{ color: #fff; }}
.val-muted {{ color: var(--gray); }}

/* Table */
.card h2 {{
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.3px;
  margin-bottom: 16px;
  color: #fff;
}}
table {{ width: 100%; border-collapse: collapse; }}
th {{
  background: var(--slate);
  color: #fff;
  padding: 10px 14px;
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
th:first-child {{ border-radius: 8px 0 0 0; }}
th:last-child {{ border-radius: 0 8px 0 0; }}
td {{
  padding: 12px 14px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 13px;
  color: rgba(255,255,255,0.85);
}}
tr:hover {{ background: var(--row-hover); }}
.status-ok {{ color: var(--light-purple); font-weight: 700; }}
.status-fail {{ color: var(--pink); font-weight: 700; }}
.error-cell {{ font-size: 11px; color: var(--gray); }}

/* Footer */
.footer {{
  text-align: center;
  color: var(--gray);
  font-size: 11px;
  padding: 24px 0 8px;
  letter-spacing: 0.3px;
}}
.footer a {{ color: var(--light-purple); text-decoration: none; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1><span class="kw">Batch</span> processing report</h1>
    <div class="subtitle">{mode_display} &middot; {summary['timestamp'][:19].replace('T', ' ')}</div>
  </div>

  <div class="card">
    <div class="stats">
      <div class="stat"><div class="stat-val val-neutral">{summary['total_documents']}</div><div class="stat-lbl">Documents</div></div>
      <div class="stat"><div class="stat-val val-accent">{summary['successful']}</div><div class="stat-lbl">Succeeded</div></div>
      <div class="stat"><div class="stat-val {"val-muted" if summary['failed'] == 0 else "val-error"}">{summary['failed']}</div><div class="stat-lbl">Failed</div></div>
      <div class="stat"><div class="stat-val {sr_class}">{sr}%</div><div class="stat-lbl">Success rate</div></div>
      <div class="stat"><div class="stat-val val-neutral">{format_duration(summary['total_duration'])}</div><div class="stat-lbl">Total time</div></div>
      <div class="stat"><div class="stat-val val-neutral">{format_duration(summary['avg_duration'])}</div><div class="stat-lbl">Avg / doc</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Document results</h2>
    <table>
      <thead><tr><th>Document</th><th>Size</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
      <tbody>{doc_rows}</tbody>
    </table>
  </div>

  <div class="footer">Generated by <a href="https://upstage.ai">Upstage</a> Batch Processor v{__version__}</div>

</div>
</body>
</html>"""

    with open(report_path, "w") as f:
        f.write(html)
    return report_path


# =============================================================================
# DRY RUN
# =============================================================================

def dry_run(documents: List[Path], mode: str, logger: logging.Logger,
            **kwargs):
    """Show what would be processed without making any API calls."""
    total_size = sum(d.stat().st_size for d in documents)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"DRY RUN — {mode}")
    logger.info(f"{'=' * 60}")
    logger.info(f"  Documents:  {len(documents)}")
    logger.info(f"  Total size: {format_size(total_size)}")

    if mode == "v2-agent":
        logger.info(f"  Agent ID:   {kwargs.get('agent_id', '?')}")
        if kwargs.get("config_id"):
            logger.info(f"  Config ID:  {kwargs['config_id']}")

    logger.info(f"\n  Documents to process:")
    for i, doc in enumerate(documents, 1):
        logger.info(f"    {i:3d}. {doc.name}  ({format_size(doc.stat().st_size)})")

    logger.info(f"\n  No API calls will be made.")
    logger.info(f"{'=' * 60}\n")


# =============================================================================
# CONFIG FILE
# =============================================================================

def load_config(config_path: Path) -> Dict:
    """Load a JSON config file."""
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ERROR: Could not load config file {config_path}: {e}")
        sys.exit(1)


def generate_sample_config(output_path: Path):
    """Write a sample config file."""
    sample = {
        "_comment": "Upstage Batch Processor config. CLI flags override these values.",
        "mode": "agent",
        "agent_id": "agt_XXXXXXXXXXXXX",
        "config_id": None,
        "docs": "./documents/",
        "output": "./batch_output/",
        "parallel": 3,
        "include": "last",
        "report": True,
        "v1_options": {
            "_comment": "Only used for v1-parse, v1-extract, v1-classify modes",
            "model": "information-extract",
            "schema": "./my_schema.json",
            "parse_model": "document-parse",
            "ocr": "auto",
            "coordinates": True,
            "output_formats": ["html", "text", "markdown"],
        },
        "retry": {
            "max_retries": 3,
            "base_delay": 5,
            "max_delay": 60,
            "backoff_factor": 2,
        },
        "poll": {
            "interval": 3,
            "max_wait": 2400,
        },
    }
    with open(output_path, "w") as f:
        json.dump(sample, f, indent=2)
    print(f"Sample config written to: {output_path}")


# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upstage_batch",
        description="Upstage Batch Processor — process documents through Studio Agents (V2) or direct V1 APIs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # V2 Studio Agent
  python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./invoices/

  # V2 with config file (CLI flags override config)
  python3 upstage_batch.py agent --config my_config.json

  # V1 parse
  python3 upstage_batch.py v1-parse --docs ./invoices/

  # V1 extract with schema
  python3 upstage_batch.py v1-extract --docs ./invoices/ --schema schema.json

  # V1 classify with schema
  python3 upstage_batch.py v1-classify --docs ./invoices/ --schema classify_schema.json

  # Dry run
  python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./invoices/ --dry-run

  # Resume interrupted batch
  python3 upstage_batch.py agent --agent-id agt_XXXXX --docs ./invoices/ --resume

  # Generate sample config
  python3 upstage_batch.py init-config
        """,
    )

    sub = parser.add_subparsers(dest="command", help="Processing mode")

    # -- agent (V2) --
    p_agent = sub.add_parser("agent", help="V2 Studio Agent batch processing (primary mode)")
    p_agent.add_argument("--agent-id", "--agent", type=str, dest="agent_id",
                         help="Studio agent ID (agt_XXXXX)")
    p_agent.add_argument("--config-id", type=str, help="Agent config version ID (optional)")
    p_agent.add_argument("--include", nargs="+", default=["last"],
                         help="Include step(s): last, all, or per-step filters (output:parse, output:classify, output:extract, output:instruct)")
    _add_common_args(p_agent)

    # -- agent-clone --
    p_clone = sub.add_parser("agent-clone", help="Clone a Studio Agent")
    p_clone.add_argument("--source-agent-id", "--source", "--from-id", type=str, required=True,
                         dest="source_agent_id", help="Source agent ID to clone (agt_XXXXX)")
    p_clone.add_argument("--name", type=str, required=True, help="Name for the cloned agent")
    p_clone.add_argument("--with-jobs", action="store_true", default=False,
                         help="Include job data in clone")
    p_clone.add_argument("--config-id", type=str, default=None,
                         help="Clone only this specific config (cfg_XXXXX)")
    p_clone.add_argument("--source-filter", type=str, default=None, dest="source_filter",
                         help="Clone only jobs with this source (e.g., 'studio', 'api')")
    p_clone.add_argument("--key", type=str, default=None,
                         help="Upstage API key (or set UPSTAGE_API_KEY env var)")
    p_clone.add_argument("--dry-run", action="store_true", help="Print payload without calling API")
    p_clone.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # -- stats --
    p_stats = sub.add_parser("stats", help="Get job statistics")
    p_stats.add_argument("--agent-id", "--agent", type=str, default=None, dest="agent_id",
                         help="Filter by agent ID")
    p_stats.add_argument("--config-id", type=str, default=None,
                         help="Filter by config ID")
    p_stats.add_argument("--source", type=str, default=None,
                         help="Filter by source (studio, api)")
    p_stats.add_argument("--since", type=str, default=None,
                         help="Jobs created after this date (ISO 8601, e.g., 2026-01-01)")
    p_stats.add_argument("--custom-only", action="store_true", default=False,
                         help="Only custom agents (exclude built-in)")
    p_stats.add_argument("--key", type=str, default=None,
                         help="Upstage API key (or set UPSTAGE_API_KEY env var)")
    p_stats.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # -- v1-parse --
    p_parse = sub.add_parser("v1-parse", help="V1 Document Parse (fallback)")
    p_parse.add_argument("--model", type=str, default="document-parse",
                         help="Parse model (default: document-parse)")
    p_parse.add_argument("--ocr", choices=["auto", "force"], default="auto")
    p_parse.add_argument("--output-formats", type=str, default="html,text,markdown",
                         help="Comma-separated output formats")
    _add_common_args(p_parse)

    # -- v1-extract --
    p_extract = sub.add_parser("v1-extract", help="V1 Information Extraction (fallback)")
    p_extract.add_argument("--schema", type=Path, required=True,
                           help="Path to extraction schema JSON")
    p_extract.add_argument("--model", type=str, default="information-extract",
                           help="IE model (default: information-extract)")
    _add_common_args(p_extract)

    # -- v1-classify --
    p_classify = sub.add_parser("v1-classify", help="V1 Document Classification (fallback)")
    p_classify.add_argument("--schema", type=Path, required=True,
                            help="Path to classification schema JSON")
    _add_common_args(p_classify)

    # -- init-config --
    p_init = sub.add_parser("init-config", help="Generate a sample config file")
    p_init.add_argument("--output", type=Path, default=Path("batch_config.json"),
                        help="Output path (default: batch_config.json)")

    return parser


def _add_common_args(p):
    """Add arguments common to all processing modes."""
    p.add_argument("--docs", type=Path, help="Path to document(s) — file or directory")
    p.add_argument("--output", type=Path, help="Output directory (default: ./batch_output_TIMESTAMP)")
    p.add_argument("--workers", "--parallel", type=int, default=5, dest="parallel",
                   help="Parallel workers (default: 5)")
    p.add_argument("--key", type=str, default=None,
                   help="Upstage API key (or set UPSTAGE_API_KEY env var)")
    p.add_argument("--config", type=Path, help="Path to JSON config file")
    p.add_argument("--dry-run", action="store_true", help="Show plan without making API calls")
    p.add_argument("--resume", action="store_true", help="Resume from checkpoint, skip completed docs")
    p.add_argument("--report", action="store_true", help="Generate HTML report")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose console output")


def _interactive_wizard():
    """Guided setup when script is run with no arguments."""
    print()
    if _USE_COLOR:
        print(f"  {_Ansi.MAGENTA}{_Ansi.BOLD}Upstage Batch Processor{_Ansi.RESET} {_Ansi.DIM}v{__version__}{_Ansi.RESET}")
        print(f"  {_Ansi.DIM}{'─' * 45}{_Ansi.RESET}")
        print(f"  {_Ansi.CYAN}Interactive Setup{_Ansi.RESET} — answer a few questions to start\n")
    else:
        print(f"  Upstage Batch Processor v{__version__}")
        print(f"  {'─' * 45}")
        print(f"  Interactive Setup — answer a few questions to start\n")

    # 1. Mode
    print("  Processing mode:")
    print("    [1] Studio Agent (V2) — recommended")
    print("    [2] Document Parse (V1)")
    print("    [3] Information Extract (V1)")
    print("    [4] Document Classify (V1)")
    mode_choice = input("\n  Choose [1-4, default=1]: ").strip() or "1"
    mode_map = {"1": "agent", "2": "v1-parse", "3": "v1-extract", "4": "v1-classify"}
    command = mode_map.get(mode_choice, "agent")

    # 2. API Key
    env_key = os.environ.get("UPSTAGE_API_KEY", "").strip()
    if env_key:
        masked = env_key[:5] + "..." + env_key[-4:]
        use_env = input(f"\n  API key found in env: {masked}. Use it? [Y/n]: ").strip().lower()
        if use_env in ("", "y", "yes"):
            api_key = env_key
        else:
            api_key = input("  Enter API key: ").strip()
    else:
        api_key = input("\n  Enter Upstage API key (up_...): ").strip()

    if not api_key:
        print("  ERROR: API key is required.")
        sys.exit(1)

    # 3. Documents path
    print()
    docs_input = input("  Path to documents (folder or file): ").strip()
    # Handle drag-and-drop paths (may have quotes or trailing spaces)
    docs_input = docs_input.strip("'\"")
    docs_path = Path(os.path.expanduser(docs_input))
    if not docs_path.exists():
        print(f"  ERROR: Path not found: {docs_path}")
        sys.exit(1)

    # 4. Agent-specific
    agent_id = None
    config_id = None
    schema_path = None
    if command == "agent":
        agent_id = input("  Studio Agent ID (agt_...): ").strip()
        if not agent_id:
            print("  ERROR: Agent ID is required for Studio Agent mode.")
            sys.exit(1)
        config_id_input = input("  Config version ID [press Enter to skip]: ").strip()
        config_id = config_id_input or None
    elif command in ("v1-extract", "v1-classify"):
        schema_input = input("  Path to schema JSON file: ").strip().strip("'\"")
        schema_path = Path(os.path.expanduser(schema_input))
        if not schema_path.exists():
            print(f"  ERROR: Schema not found: {schema_path}")
            sys.exit(1)

    # 5. Workers
    workers_input = input(f"\n  Number of parallel workers [default=5]: ").strip()
    workers = int(workers_input) if workers_input.isdigit() else 5

    # 6. Resume?
    resume = False
    resume_input = input("  Resume from previous checkpoint? [y/N]: ").strip().lower()
    if resume_input in ("y", "yes"):
        resume = True

    # 7. Confirm
    documents = find_documents(docs_path)
    print()
    if _USE_COLOR:
        print(f"  {_Ansi.DIM}{'─' * 45}{_Ansi.RESET}")
    else:
        print(f"  {'─' * 45}")
    print(f"  Mode:       {command}")
    print(f"  Documents:  {len(documents)} found in {docs_path}")
    if agent_id:
        print(f"  Agent:      {agent_id}")
    print(f"  Workers:    {workers}")
    print(f"  Resume:     {'yes' if resume else 'no'}")
    if _USE_COLOR:
        print(f"  {_Ansi.DIM}{'─' * 45}{_Ansi.RESET}")
    else:
        print(f"  {'─' * 45}")

    if not documents:
        print(f"\n  ERROR: No supported documents found at {docs_path}")
        print(f"  Supported types: {', '.join(sorted(DOC_EXTENSIONS))}")
        sys.exit(1)

    confirm = input(f"\n  Start processing? [Y/n]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("  Cancelled.")
        sys.exit(0)

    # Build a fake args namespace
    class Args:
        pass
    args = Args()
    args.command = command
    args.docs = docs_path
    args.output = None
    args.parallel = workers
    args.key = api_key
    args.config = None
    args.dry_run = False
    args.resume = resume
    args.report = False
    args.verbose = False
    if command == "agent":
        args.agent_id = agent_id
        args.config_id = config_id
        args.include = ["last"]
    elif command == "v1-parse":
        args.model = "document-parse"
        args.ocr = "auto"
        args.output_formats = "html,text,markdown"
    elif command == "v1-extract":
        args.schema = schema_path
        args.model = "information-extract"
    elif command == "v1-classify":
        args.schema = schema_path

    return args


def main():
    # Install SIGINT/SIGTERM handlers before anything else so a Ctrl+C during
    # slow startup (file discovery, big folders) still exits cleanly.
    install_signal_handlers()

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        # No subcommand → launch interactive wizard
        args = _interactive_wizard()

    # -- init-config --
    if args.command == "init-config":
        generate_sample_config(args.output)
        return

    # -- agent-clone --
    if args.command == "agent-clone":
        api_key = get_api_key(getattr(args, "key", None))
        log_verbose = getattr(args, "verbose", False)
        logger = setup_logging(Path("."), log_verbose)

        result = v2_clone_agent(
            source_agent_id=args.source_agent_id,
            name=args.name,
            api_key=api_key,
            with_jobs=args.with_jobs,
            config_id=getattr(args, "config_id", None),
            source=getattr(args, "source_filter", None),
            dry_run=getattr(args, "dry_run", False),
            logger=logger,
        )

        if result.get("dry_run"):
            return
        if result.get("success"):
            print(f"\n  Clone successful!")
            print(f"  New Agent ID:     {result['agent_id']}")
            print(f"  Name:             {result.get('name', '?')}")
            print(f"  Cloned from:      {result.get('source_agent_id', '?')}")
            print(f"\n  Use the new agent ID in your batch runs.")
        else:
            print(f"\n  Clone failed: {result.get('error', 'unknown')}")
            sys.exit(1)
        return

    # -- stats --
    if args.command == "stats":
        api_key = get_api_key(getattr(args, "key", None))
        log_verbose = getattr(args, "verbose", False)
        logger = setup_logging(Path("."), log_verbose)

        result = v2_get_stats(
            api_key=api_key,
            agent_id=getattr(args, "agent_id", None),
            config_id=getattr(args, "config_id", None),
            source=getattr(args, "source", None),
            since=getattr(args, "since", None),
            is_custom=True if getattr(args, "custom_only", False) else None,
            logger=logger,
        )

        if result.get("error") or result.get("success") is False:
            print(f"\n  Stats request failed: {result.get('error', 'unknown')}")
            sys.exit(1)

        jobs = result.get("jobs", {})
        in_prog = jobs.get("in_progress", 0)
        completed = jobs.get("completed", 0)
        failed = jobs.get("failed", 0)
        cached = jobs.get("cached", 0)
        total = in_prog + completed + failed

        print(f"\n  {'=' * 40}")
        print(f"  Job Statistics")
        if getattr(args, "agent_id", None):
            print(f"  Agent: {args.agent_id}")
        if getattr(args, "since", None):
            print(f"  Since: {args.since}")
        print(f"  {'=' * 40}")
        print(f"  Total jobs:    {total}")
        print(f"  Completed:     {completed}")
        print(f"  Failed:        {failed}")
        print(f"  In progress:   {in_prog}")
        print(f"  Cached:        {cached}")
        if total > 0:
            success_rate = completed / total * 100
            print(f"  Success rate:  {success_rate:.1f}%")
        print(f"  {'=' * 40}")
        return

    # -- Load config file if provided, CLI overrides --
    cfg = {}
    if hasattr(args, "config") and args.config:
        config_path = Path(os.path.expanduser(str(args.config))).resolve()
        cfg = load_config(config_path)

    # Resolve docs path — expand ~, resolve relative paths to absolute
    docs_raw = args.docs or Path(cfg.get("docs", "."))
    docs_path = Path(os.path.expanduser(str(docs_raw))).resolve()
    if not docs_path.exists():
        print(f"ERROR: Documents path does not exist: {docs_path}")
        sys.exit(1)

    documents = find_documents(docs_path)
    if not documents:
        print(f"ERROR: No supported documents found at {docs_path}")
        print(f"Supported types: {', '.join(sorted(DOC_EXTENSIONS))}")
        sys.exit(1)

    # Resolve output dir — default to a subfolder next to the docs
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        output_dir = Path(os.path.expanduser(str(args.output))).resolve()
    elif cfg.get("output"):
        output_dir = Path(os.path.expanduser(cfg["output"])).resolve()
    else:
        # Place output inside the docs folder so results live with the documents
        if docs_path.is_dir():
            output_dir = docs_path / f"batch_output_{ts}"
        else:
            output_dir = docs_path.parent / f"batch_output_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    parallel = args.parallel if args.parallel != 5 else cfg.get("parallel", 5)
    do_report = args.report or cfg.get("report", False)
    verbose = getattr(args, "verbose", False)

    # Setup logging
    logger = setup_logging(output_dir, verbose)
    api_key = get_api_key(getattr(args, "key", None))

    # Startup banner
    if HAS_RICH:
        console = Console()
        console.print(f"\n  [bold magenta]Upstage Batch Processor[/bold magenta] [dim]v{__version__}[/dim]")
        console.print(f"  [dim]{'─' * 45}[/dim]")
        console.print(f"  [dim]Documents:[/dim]  {len(documents)} found in {docs_path}")
        console.print(f"  [dim]Output:[/dim]     {output_dir}")
        console.print(f"  [dim]Workers:[/dim]    {parallel}")
        if HAS_RICH:
            console.print(f"  [dim]Rich:[/dim]       [green]enabled[/green]  (pip install rich)")
        console.print()
    else:
        logger.info(f"\n  {_c(_Ansi.MAGENTA + _Ansi.BOLD, 'Upstage Batch Processor')} {_c(_Ansi.DIM, 'v' + __version__)}")
        logger.info(f"  {_c(_Ansi.DIM, '─' * 45)}")
        logger.info(f"  {_c(_Ansi.DIM, 'Documents:')}  {len(documents)} found in {docs_path}")
        logger.info(f"  {_c(_Ansi.DIM, 'Output:')}     {output_dir}")
        logger.info(f"  {_c(_Ansi.DIM, 'Workers:')}    {parallel}")
        logger.info(f"  {_c(_Ansi.DIM, 'Tip:')}        pip install rich  for enhanced progress display")
        logger.info("")

    # Pre-upload validation: reject zero-byte files and PDFs with corrupt
    # headers before they hit the API. Saves upload quota and keeps the log
    # clean of confusing HTTP 415 errors for files that were bad at source.
    if not getattr(args, "dry_run", False):
        before_prescreen = len(documents)
        documents = prescreen_documents(documents, output_dir, logger)
        if not documents:
            logger.error(
                f"All {before_prescreen} file(s) failed pre-upload validation. "
                f"Nothing to process. See skipped_files.json for details."
            )
            return

    # Checkpoint
    checkpoint = None
    if getattr(args, "resume", False):
        checkpoint = Checkpoint(output_dir / ".checkpoint.json")
        before = len(documents)
        documents = checkpoint.remaining(documents)
        skipped = before - len(documents)
        if skipped > 0:
            logger.info(f"Resuming: skipping {skipped} already-completed document(s)")
        if not documents:
            logger.info("All documents already completed. Nothing to do.")
            return
    elif not getattr(args, "dry_run", False):
        checkpoint = Checkpoint(output_dir / ".checkpoint.json")

    # Retry and poll config from file
    retry_config = cfg.get("retry", DEFAULT_RETRY)
    poll_config = cfg.get("poll", DEFAULT_POLL)

    start_time = time.time()

    # =========================================================================
    # DISPATCH
    # =========================================================================

    if args.command == "agent":
        agent_id = args.agent_id or cfg.get("agent_id")
        if not agent_id:
            print("ERROR: --agent-id is required (or set agent_id in config file)")
            sys.exit(1)

        config_id = getattr(args, "config_id", None) or cfg.get("config_id")
        # include is now a List[str] from nargs="+"
        include = args.include if args.include != ["last"] else cfg.get("include", ["last"])
        if isinstance(include, str):
            # Handle config file where include might be a string
            include = include.split() if " " in include else [include]
        mode = "v2-agent"

        if getattr(args, "dry_run", False):
            dry_run(documents, mode, logger, agent_id=agent_id, config_id=config_id)
            return

        def process_fn(doc, **kw):
            return v2_process_one(doc, agent_id, api_key, config_id, include,
                                  poll_config, logger)

        results = run_batch(process_fn, documents, parallel, checkpoint, logger, output_dir)

    elif args.command == "v1-parse":
        mode = "v1-parse"
        options = {
            "model": args.model,
            "ocr": args.ocr,
            "coordinates": True,
            "output_formats": args.output_formats.split(","),
        }

        if getattr(args, "dry_run", False):
            dry_run(documents, mode, logger)
            return

        def process_fn(doc, **kw):
            return v1_parse_one(doc, api_key, options, logger)

        results = run_batch(process_fn, documents, parallel, checkpoint, logger, output_dir)

    elif args.command == "v1-extract":
        mode = "v1-extract"
        schema = load_config(Path(os.path.expanduser(str(args.schema))).resolve())
        model = args.model

        if getattr(args, "dry_run", False):
            dry_run(documents, mode, logger)
            return

        def process_fn(doc, **kw):
            return v1_extract_one(doc, schema, api_key, model, logger)

        results = run_batch(process_fn, documents, parallel, checkpoint, logger, output_dir)

    elif args.command == "v1-classify":
        mode = "v1-classify"
        schema = load_config(Path(os.path.expanduser(str(args.schema))).resolve())

        if getattr(args, "dry_run", False):
            dry_run(documents, mode, logger)
            return

        def process_fn(doc, **kw):
            return v1_classify_one(doc, schema, api_key, logger)

        results = run_batch(process_fn, documents, parallel, checkpoint, logger, output_dir)

    else:
        parser.print_help()
        return

    # Generate summary and report
    summary = generate_summary(results, mode, start_time, output_dir, logger)

    if do_report:
        report_path = generate_html_report(results, summary, output_dir)
        logger.info(f"HTML report: {report_path}")


if __name__ == "__main__":
    main()
