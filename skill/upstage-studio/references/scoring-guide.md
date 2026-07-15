# Scoring extraction accuracy

`scripts/score.py` tells you how well the extraction matches ground truth *you*
define. It's the objective feedback loop for schema iteration: run your docs,
score them, read the per-field misses, sharpen the schema, repeat.

## What it measures

For every field, on every document, you get two numbers:

- **Raw** — exact string match between ground truth and extraction.
- **Normalized** — the same comparison after your normalization rules (trim
  whitespace, drop commas, standardize casing, etc.) are applied to **both**
  sides. Seeing raw and normalized side by side tells you how much of any gap is
  just formatting vs. genuine content.

The headline is a **micro-accuracy** over "slots" — every ground-truth value is
one slot:

```
accuracy = true_positives / (true_positives + false_positives + false_negatives)
```

Each field-on-a-document lands in one of these buckets:

| Ground truth | Extraction | Outcome |
|--------------|-----------|---------|
| present | matches | true positive |
| present | different value | mismatch (counts as both a miss and an extra) |
| present | blank/absent | miss (false negative) |
| absent | has a value | extra (false positive) |
| absent | absent | agreement on absence |

By default, "both absent" is left out of the score entirely (`tn_policy:
"exclude"`) so a schema with many optional fields isn't flattered by all the
blanks it correctly leaves blank. You can set `tn_policy: "include"` to count
them.

Long string fields (≥ 50 chars) also get a character-level similarity check, so
a near-perfect long value that misses on one character is flagged as
"close but not exact" rather than silently counted wrong.

The metric here is a simple, transparent micro-accuracy — not a
reimplementation of any published metric. If you want a rigorous,
structure-aware KIE metric, see Khang et al., *KIEval: Evaluation Metric for
Document Key Information Extraction* (arXiv:2503.05488).

## Ground-truth format

One small JSON file per document, in your ground-truth folder:

```json
{
  "doc": "01_triangle.pdf",
  "ground_truth": {
    "applicant_name": "Triangle Contracting LLC",
    "years_in_business": 22,
    "total_receipts": 4200000,
    "business_start_date": "",
    "loss_history": [
      { "carrier": "Acme Mutual", "claim_amount": 12000 },
      { "carrier": "Beacon Insurance", "claim_amount": 0 }
    ]
  }
}
```

- Use `""` (empty string) for a field that genuinely isn't present on that
  document — that's what lets the scorer credit the model for correctly leaving
  it blank.
- Array fields hold a list of row objects.
- Write ground truth from the **document**, independently of what the model
  produced, so the score stays honest.

## Scoring config

`score.py --config scoring_config.json` reads a small config
(`examples/scoring_config.example.json` is a ready template):

```json
{
  "ground_truth_dir": "gt/",
  "extractions_dir": "results/",
  "normalization_rules_file": "normalization_rules.json",
  "fields": ["applicant_name", "years_in_business", "total_receipts", "loss_history"],
  "field_config": {
    "loss_history": { "type": "array" }
  },
  "tn_policy": "exclude",
  "accuracy_threshold": 0.9,
  "output_report": "accuracy_report.md"
}
```

- `extractions_dir` is where `run_agent.py --output` wrote its per-document JSON
  (the scorer reads either a plain `{field: value}` object or the step-list
  `run_agent.py` writes and finds the extraction automatically).
- List array-of-object fields under `field_config` with `"type": "array"`.
- Omit `fields` to score every field present in ground truth.
- Fields below `accuracy_threshold` are flagged in the report so they're easy to
  spot.

## Array (table) fields

By default, array-of-object fields are matched **row by row, in order** — the
first ground-truth row is compared to the first extracted row, and so on. If the
model returns the right rows in a different order, positional matching will
under-count them. When a table has a natural key (a carrier name, a policy
number), set that key so rows are matched by identity instead of position:

```json
"field_config": {
  "loss_history": { "type": "array", "match": "key", "key": "carrier" }
}
```

With `match: "key"`, each ground-truth row is paired with the extracted row that
shares the same key value, and any extra extracted rows count as hallucinations.
Use positional matching (the default) when rows have no stable key.

## Normalization rules (optional)

A small registry so both sides are compared on equal footing:

```json
{
  "rules": [
    { "scope": "*", "operations": ["strip", "collapse_whitespace"] },
    { "scope": "total_receipts", "operations": ["remove_currency"] },
    { "scope": "phone", "operations": ["digits_only"] }
  ]
}
```

`scope: "*"` applies to every field; a field name applies to that field only.
Available operations: `strip`, `lower`, `collapse_whitespace`, `remove_commas`,
`remove_currency`, `digits_only`, `strip_punctuation`. Keep the list small and
deliberate — a normalization rule should reflect a formatting choice you'd
genuinely accept, not paper over a real error.

## The report

`score.py` prints and writes a Markdown report with the overall raw/normalized
accuracy, a per-field table (sorted worst-first, with miss/extra counts), a
per-document table, and a list of any "close but not exact" long strings. Point
your next iteration at the fields at the top of that per-field table.
