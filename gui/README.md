# Upstage Batch Processor — CLI + Web GUI

A zero-dependency tool for running a folder of documents through an Upstage AI
Studio Agent (or the V1 APIs), with a friendly local web interface. Built for
users who'd rather click and drag than write code.

## Run it

```bash
python3 upstage_batch_gui.py
```

Your browser opens automatically. Paste your API key (created in the Upstage
Console at [console.upstage.ai](https://console.upstage.ai)), your Agent ID
(`agt_...`, from [studio.upstage.ai](https://studio.upstage.ai)) and optional
Config ID (`cfg_...`), pick a folder of documents, and click **Run Batch**.
Progress streams live; when it finishes you get a sortable results table with a
click-to-expand JSON view per document.

Prefer the command line?

```bash
python3 upstage_batch.py agent --agent-id agt_XXX --docs ./documents/ --config-id cfg_XXX
```

Both read your API key from `--key` or the `UPSTAGE_API_KEY` environment
variable. Requires Python 3.8+ and nothing else (optional `rich` for prettier
CLI output, optional `pikepdf` for auto-repairing corrupted PDF headers).

## Notes on how it runs

- The GUI is a local server bound to `127.0.0.1` and only answers requests whose
  `Host` header is loopback, so a web page you happen to have open can't reach
  it. Your API key is passed to the batch process through the environment, never
  on the command line.
- It works fully offline against the Upstage API — no third-party assets are
  fetched to render the UI.
- Results, logs, and checkpoints are written to an output folder next to your
  documents (or a folder you choose). Interrupted runs resume with `--resume`.
- Each document's result JSON carries every step the Agent ran (`steps`, in
  order) and a per-step-name list (`extracted`), so split children and
  validate / merge steps are all there.
- `upstage_batch.py` ships inside the GUI zip next to `upstage_batch_gui.py`.
  In a clone of the toolkit repo it is not in `gui/` — the GUI resolves it from
  `skill/upstage-studio/scripts/upstage_batch.py` instead, so a plain clone
  runs without a build step.
- A document whose job fails still gets a result JSON: every step that
  completed before the failure is there under `steps` / `extracted`, with the
  error code and the failing step recorded, so nothing already paid for is lost.
