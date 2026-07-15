#!/usr/bin/env python3
"""
score.py — Measure extraction accuracy against your own ground truth.

Compares a folder of extraction results (one JSON per document) to a folder of
ground-truth files, field by field, and writes a Markdown report. For every
field you get two numbers: RAW (exact match) and NORMALIZED (after your
normalization rules are applied to both sides). Standard library only.

The metric is a simple, transparent micro-accuracy over "slots": each
ground-truth value is one slot, and the headline is TP / (TP + FP + FN). It is
a lightweight metric in the spirit of key-information-extraction evaluation
(see Khang et al., "KIEval: Evaluation Metric for Document Key Information
Extraction," arXiv:2503.05488) — not a reimplementation of that paper's
structure-aware edit-cost metric.

Usage
-----
  python score.py --config scoring_config.json

See references/scoring-guide.md and examples/scoring_config.example.json.
"""

import argparse
import json
import os
import re
from difflib import SequenceMatcher

# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Top-level keys that mark a per-document result *wrapper* (from the batch
# runner) rather than the extraction fields themselves.
_WRAPPER_KEYS = {
    "document", "file_path", "file_size", "mode", "agent_id", "success",
    "timestamp", "duration_seconds", "job_id", "file_id", "status", "usage",
    "error", "error_code", "extracted", "final_output", "raw_response",
    "recoverable",
}


def candidate_dicts(obj):
    """Collect every plausible 'extracted fields' dict from any supported
    result shape: a plain {field: value} dict, run_agent.py's step-list, or a
    batch-runner wrapper ({document, success, extracted, final_output, ...})."""
    cands = []
    if isinstance(obj, dict):
        # run_agent single-step {step_index, step_id, output}
        if "output" in obj and set(obj) <= {"step_index", "step_id", "output"}:
            return candidate_dicts(obj["output"])
        # batch-runner wrapper: pull the per-step data + the final output
        if {"extracted", "final_output"} & set(obj) and _WRAPPER_KEYS & set(obj):
            fo = obj.get("final_output")
            if isinstance(fo, dict):
                cands.append(fo)
            extracted = obj.get("extracted")
            if isinstance(extracted, dict):
                for step in extracted.values():
                    data = step.get("data") if isinstance(step, dict) else step
                    if isinstance(data, dict):
                        cands.append(data)
            return cands
        # a plain field dict
        return [obj]
    if isinstance(obj, list):
        for step in obj:
            out = step.get("output") if isinstance(step, dict) else None
            if isinstance(out, dict):
                cands.append(out)
    return cands


def resolve_extraction(obj, gt_fields):
    """Pick the candidate dict that best matches the ground-truth field set.
    This makes scoring robust to result shape (run_agent vs. batch runner) and
    to an instruct/gate step that also emits JSON — we score the dict that
    actually looks like the extraction, not just 'the last one'."""
    cands = candidate_dicts(obj)
    if not cands:
        return {}
    if gt_fields:
        gt_keys = set(gt_fields)
        cands.sort(key=lambda d: len(gt_keys & set(d.keys())), reverse=True)
    return cands[0]


def load_ground_truth(gt_dir):
    """Each GT file: {"doc": "<filename>", "ground_truth": {field: value}}.
    Returns {doc_stem: {field: value}}."""
    gt = {}
    for name in sorted(os.listdir(gt_dir)):
        if not name.endswith(".json"):
            continue
        data = load_json(os.path.join(gt_dir, name))
        doc = data.get("doc") or os.path.splitext(name)[0]
        stem = os.path.splitext(os.path.basename(doc))[0]
        gt[stem] = data.get("ground_truth", data)
    return gt


def load_extractions(ex_dir):
    """Return {doc_stem: raw_loaded_json}. The extraction fields are resolved
    later (in score) once we know the ground-truth field set."""
    out = {}
    for name in sorted(os.listdir(ex_dir)):
        if not name.endswith(".json"):
            continue
        stem = os.path.splitext(name)[0]
        out[stem] = load_json(os.path.join(ex_dir, name))
    return out


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

OPS = {
    "strip": lambda s: s.strip(),
    "lower": lambda s: s.lower(),
    "collapse_whitespace": lambda s: re.sub(r"\s+", " ", s).strip(),
    "remove_commas": lambda s: s.replace(",", ""),
    "remove_currency": lambda s: re.sub(r"[\$,]", "", s).strip(),
    "digits_only": lambda s: re.sub(r"\D", "", s),
    "strip_punctuation": lambda s: re.sub(r"[^\w\s]", "", s),
}


def build_normalizer(rules):
    """rules: list of {"scope": "<field>|*", "operations": ["strip", ...]}.
    Returns a fn(field_name, value_str) -> normalized_str."""
    by_field, glob = {}, []
    for rule in rules or []:
        scope = rule.get("scope", "*")
        ops = rule.get("operations", [])
        (glob if scope == "*" else by_field.setdefault(scope, [])).extend(ops)

    def normalize(field, value):
        s = value
        for op in glob + by_field.get(field, []):
            fn = OPS.get(op)
            if fn:
                s = fn(s)
        return s

    return normalize


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def is_absent(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def to_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def char_f1(a, b):
    """Character-level similarity for long strings (0..1)."""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def compare_scalar(gt_val, ex_val, field, normalize):
    """Return an outcome for one scalar slot, raw and normalized.
    Outcomes: 'tp', 'miss', 'hallucination', 'wrong', 'absent_agree'."""
    gt_absent, ex_absent = is_absent(gt_val), is_absent(ex_val)
    if gt_absent and ex_absent:
        return {"raw": "absent_agree", "norm": "absent_agree", "close": False}
    if gt_absent and not ex_absent:
        return {"raw": "hallucination", "norm": "hallucination", "close": False}
    if not gt_absent and ex_absent:
        return {"raw": "miss", "norm": "miss", "close": False}

    g, e = to_str(gt_val), to_str(ex_val)
    raw = "tp" if g == e else "wrong"
    gn, en = normalize(field, g), normalize(field, e)
    norm = "tp" if gn == en else "wrong"
    close = norm == "wrong" and len(gn) >= 50 and char_f1(gn, en) >= 0.85
    return {"raw": raw, "norm": norm, "close": close}


def compare_array(gt_list, ex_list, field, normalize, match="positional", key=None):
    """Compare two arrays of row objects, cell by cell.

    match="positional" (default): row i of GT vs row i of the extraction.
    match="key": align rows by a shared key field (e.g. carrier name), so a
    correct-but-reordered table still scores correctly. Unmatched extraction
    rows count as hallucinated cells.

    Returns a list of scalar outcomes (one per inner cell across all rows)."""
    gt_list = gt_list or []
    ex_list = ex_list or []
    outcomes = []

    if match == "key" and key:
        ex_by_key = {}
        for row in ex_list:
            if isinstance(row, dict):
                ex_by_key.setdefault(to_str(row.get(key)), row)
        matched = set()
        for g_row in gt_list:
            g_row = g_row if isinstance(g_row, dict) else {}
            k = to_str(g_row.get(key))
            e_row = ex_by_key.get(k, {})
            if k in ex_by_key:
                matched.add(k)
            for kk in sorted(set(g_row) | set(e_row)):
                outcomes.append(
                    compare_scalar(g_row.get(kk), e_row.get(kk), f"{field}.{kk}", normalize)
                )
        # Extraction rows with no matching GT row = hallucinated cells.
        for row in ex_list:
            if not isinstance(row, dict):
                continue
            if to_str(row.get(key)) not in matched:
                for kk in sorted(row):
                    outcomes.append(
                        compare_scalar(None, row.get(kk), f"{field}.{kk}", normalize)
                    )
        return outcomes

    # Positional alignment (default).
    n = max(len(gt_list), len(ex_list))
    for i in range(n):
        g_row = gt_list[i] if i < len(gt_list) else {}
        e_row = ex_list[i] if i < len(ex_list) else {}
        keys = set(g_row) | set(e_row)
        for k in sorted(keys):
            outcomes.append(
                compare_scalar(g_row.get(k), e_row.get(k), f"{field}.{k}", normalize)
            )
    return outcomes


# --------------------------------------------------------------------------- #
# Tallying
# --------------------------------------------------------------------------- #

def tally(outcomes, tn_policy="exclude"):
    """Turn a list of outcome dicts into TP/FP/FN counts for raw and norm."""
    counts = {}
    for mode in ("raw", "norm"):
        tp = fp = fn = tn = 0
        for o in outcomes:
            r = o[mode]
            if r == "tp":
                tp += 1
            elif r == "miss":
                fn += 1
            elif r == "hallucination":
                fp += 1
            elif r == "wrong":
                fp += 1
                fn += 1
            elif r == "absent_agree":
                tn += 1
        denom = tp + fp + fn + (tn if tn_policy == "include" else 0)
        acc = (tp + (tn if tn_policy == "include" else 0)) / denom if denom else 1.0
        counts[mode] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "accuracy": acc}
    return counts


# --------------------------------------------------------------------------- #
# Main scoring
# --------------------------------------------------------------------------- #

def score(cfg):
    gt = load_ground_truth(cfg["ground_truth_dir"])
    raw_ex = load_extractions(cfg["extractions_dir"])
    field_cfg = cfg.get("field_config", {})
    tn_policy = cfg.get("tn_policy", "exclude")
    rules = []
    if cfg.get("normalization_rules_file"):
        rules = load_json(cfg["normalization_rules_file"]).get("rules", [])
    normalize = build_normalizer(rules)

    # Which fields to score: explicit list, else union of GT fields.
    fields = cfg.get("fields")
    if not fields:
        fields = sorted({k for d in gt.values() for k in d})

    # Resolve each raw result down to the extraction dict that best matches the
    # GT field set (robust to run_agent vs. batch-runner output shapes).
    ex = {stem: resolve_extraction(obj, fields) for stem, obj in raw_ex.items()}

    per_field = {f: [] for f in fields}      # field -> list of outcome dicts
    per_doc = {}                             # doc -> list of outcome dicts
    close_hits = []                          # (doc, field) close-but-not-exact

    for stem, gt_fields in sorted(gt.items()):
        ex_fields = ex.get(stem, {})
        doc_outcomes = []
        for f in fields:
            fc = field_cfg.get(f, {})
            ftype = fc.get("type", "scalar")
            gt_val, ex_val = gt_fields.get(f), ex_fields.get(f)
            if ftype == "array":
                outs = compare_array(gt_val, ex_val, f, normalize,
                                     fc.get("match", "positional"), fc.get("key"))
            else:
                outs = [compare_scalar(gt_val, ex_val, f, normalize)]
                if outs[0]["close"]:
                    close_hits.append((stem, f))
            per_field[f].extend(outs)
            doc_outcomes.extend(outs)
        per_doc[stem] = doc_outcomes

    # Overall = every scored slot across every doc.
    all_outcomes = [o for outs in per_field.values() for o in outs]
    overall = tally(all_outcomes, tn_policy)
    field_scores = {f: tally(o, tn_policy) for f, o in per_field.items()}
    doc_scores = {d: tally(o, tn_policy) for d, o in per_doc.items()}

    return {
        "overall": overall,
        "fields": field_scores,
        "docs": doc_scores,
        "close_hits": close_hits,
        "missing_extractions": sorted(set(gt) - set(ex)),
        "tn_policy": tn_policy,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def pct(x):
    return f"{x * 100:.1f}%"


def render_report(res, cfg):
    thr = cfg.get("accuracy_threshold", 0.9)
    lines = ["# Extraction Accuracy Report", ""]
    o = res["overall"]
    lines += [
        f"**Overall accuracy** (micro, TN policy = `{res['tn_policy']}`):",
        f"- Raw (exact match): **{pct(o['raw']['accuracy'])}**",
        f"- Normalized: **{pct(o['norm']['accuracy'])}**",
        "",
        f"Slots — normalized: {o['norm']['tp']} correct, "
        f"{o['norm']['fn']} missed, {o['norm']['fp']} extra/mismatched.",
        "",
    ]

    if res["missing_extractions"]:
        lines += ["> ⚠️ No extraction found for: "
                  + ", ".join(res["missing_extractions"]), ""]

    lines += ["## Per-field accuracy", "",
              "| Field | Raw | Normalized | Miss | Extra |",
              "|-------|-----|-----------|------|-------|"]
    for f, s in sorted(res["fields"].items(),
                       key=lambda kv: kv[1]["norm"]["accuracy"]):
        n = s["norm"]
        flag = "" if n["accuracy"] >= thr else " ⬅"
        lines.append(f"| `{f}`{flag} | {pct(s['raw']['accuracy'])} | "
                     f"{pct(n['accuracy'])} | {n['fn']} | {n['fp']} |")

    lines += ["", "## Per-document accuracy", "",
              "| Document | Raw | Normalized |",
              "|----------|-----|-----------|"]
    for d, s in sorted(res["docs"].items()):
        lines.append(f"| {d} | {pct(s['raw']['accuracy'])} | "
                     f"{pct(s['norm']['accuracy'])} |")

    if res["close_hits"]:
        lines += ["", "## Close but not exact (normalized char-F1 ≥ 0.85)", ""]
        for d, f in res["close_hits"]:
            lines.append(f"- {d} — `{f}`")

    lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Score extractions against ground truth.")
    p.add_argument("--config", required=True, help="Path to scoring_config.json")
    args = p.parse_args()

    cfg = load_json(args.config)
    res = score(cfg)
    report = render_report(res, cfg)

    out_path = cfg.get("output_report", "accuracy_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
