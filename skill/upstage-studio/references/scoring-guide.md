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

`tn_policy` decides what happens to "both absent". It has no default — the
scorer stops and asks rather than choosing for you, because the choice moves the
headline by whole points on a schema with many optional fields, and two runs
must never publish different-meaning numbers under the same label.

- `"exclude"` — both-blank cells are left out of the numerator *and* the
  denominator. Standard micro-accuracy, and the number to quote unless you have
  a stated reason not to: a schema with many optional fields isn't flattered by
  all the blanks it correctly leaves blank.
- `"credit"` — both-blank cells count as correct (a schema-slot view). It
  rewards correctly-left-blank fields and inflates as you add optional fields,
  so use it only for form-completion metrics and label it as such. (`"include"`
  is accepted as a synonym.)

Whichever you pick, say which one alongside the number. The report's `SCOPE:`
line carries it for you.

Long string fields (≥ 50 chars) also get a character-level similarity check, so
a near-perfect long value that misses on one character is flagged as
"close but not exact" rather than silently counted wrong.

The metric here is a simple, transparent micro-accuracy — not a
reimplementation of any published metric. If you want a rigorous,
structure-aware KIE metric, see Khang et al., *KIEval: Evaluation Metric for
Document Key Information Extraction* (arXiv:2503.05488).

## Run it more than once

Extraction is not deterministic. Run the same documents through the same config
twice and the numbers move. So **a single run is a draw, not a measurement** —
and the first thing to establish about any config is how far its own draws
spread, because that spread is the bar every later change has to clear.

The routine:

- **Run the same config n ≥ 3 times** — n = 5 for any number you are going to
  put in front of someone — and report **mean, standard deviation and range**,
  not the best draw and not the first one.
- **Only call two configs different when the delta exceeds the run-to-run
  spread.** A change that moves the score by less than the spread you already
  measured has not been shown to do anything. Report it as noise and go looking
  for a bigger lever. It is common for the spread to be wider than the change
  you were arguing about.
- **A byte-identical repeat is a cache hit, not a replicate.** Identical file +
  identical step settings reuse the previous result for 7 days
  (`agent-api.md` — Caching). If two runs produce the same bytes, you have one
  draw, not two: change something about the input (a schema nonce, a step
  setting) to force fresh execution, and re-draw.
- **State the scope of every number**: which documents, how many cells, which
  ground-truth version, which model, and n. `87% — 12 docs, 340 cells, GT v3,
  <model>, n=5 (sd 1.4)` is a result; a bare `87%` is not.

`score.py` supports this directly. Point one config at each run's output folder
rather than cloning the config file per run — same configuration, different
draw, which is exactly what makes the runs comparable:

```bash
python scripts/score.py --config scoring_config.json \
    --extractions-dir results/run1 --run-label run1
python scripts/score.py --config scoring_config.json \
    --extractions-dir results/run2 --run-label run2
python scripts/score.py --config scoring_config.json \
    --extractions-dir results/run3 --run-label run3
```

`--extractions-dir` overrides the config's `extractions_dir` for that run only;
`--run-label` writes to `accuracy_report__run1.md`, `…__run2.md` and so on, so
the reports don't overwrite each other, and prints the label on each report's
`SCOPE:` line.

## Pin the model you will deploy

Score on the model you can actually run in production. If the deployment is
in-VPC or on-prem, that is the model the number has to come from — a champion
tuned on a larger cloud model is a result you can't ship, and the gap only
surfaces at deployment. Run the stronger model as a **comparison arm** if it's
useful to know the headroom, but the headline number stays on the deployable
model, and the `SCOPE:` of every number names which model it was.

## Get the answer key first

Ground truth written without the business owner's rulings is *your opinion of
the answer*. Before building it, take the scope questions the schema forces —
"does this field include X, or only Y?", "is a blank row a row?", "which of two
dates on the page counts?" — back to whoever owns the process, and write down
the rulings. Those questions are cheap now and expensive later: every one you
answered yourself turns into a disagreement when the customer diffs your
results against their own key, and it relocates the argument from the schema
(where you can fix it) to the definition of correct (where you can't). If a
real answer key already exists on their side, start from it.

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

- `extractions_dir` is where `run_agent.py --output` (or the batch CLI's
  `results/` folder) wrote its per-document JSON. The scorer reads a plain
  `{field: value}` object or either runner's per-step output and picks the
  step whose fields best match the ground truth, so an instruct or validate
  step in the same file doesn't confuse it.
- List array-of-object fields under `field_config` with `"type": "array"`.
- Omit `fields` to score every field present in ground truth.
- `tn_policy` is **required** — see "What it measures" above. The scorer exits
  with an error naming the two choices rather than picking one silently.
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

Every report opens with a `SCOPE:` line — documents, cells compared, TN policy,
run label — above the headline, so the number can't be copied out of the report
without the scope it was measured on:

```
SCOPE: 12 docs (01_triangle, 02_beacon, …) · 340 cells compared · tn_policy=exclude · run=run1
```

Fill in the model and the `n` from your replicate set when you quote it
elsewhere.
