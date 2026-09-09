#!/usr/bin/env bash
# build.sh — assemble the two delivery artifacts from a single source of truth.
#
# The batch CLI lives ONCE, at skill/upstage-studio/scripts/upstage_batch.py
# (committed, so a plain git clone is complete). This script stamps that file
# into gui/ so the GUI zip is self-contained, compiles every script, and
# packages both zips. Run it after editing anything, before shipping.
#
# Outputs (in dist/):
#   upstage-studio.zip      — the Claude skill + scripts (technical / AI-native users)
#   upstage-batch-gui.zip   — the batch CLI + web GUI (non-technical users)
set -euo pipefail
cd "$(dirname "$0")"

SKILL_SCRIPTS="skill/upstage-studio/scripts"
CANONICAL="$SKILL_SCRIPTS/upstage_batch.py"
DIST="dist"

echo "→ Stamping canonical batch CLI ($CANONICAL) into gui/"
cp "$CANONICAL" "gui/upstage_batch.py"

echo "→ Cleaning build cruft"
find skill gui -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find skill gui -name '.DS_Store' -delete 2>/dev/null || true

echo "→ Sanity: compile every script"
python3 -m py_compile \
  "$CANONICAL" \
  gui/upstage_batch.py \
  gui/upstage_batch_gui.py \
  "$SKILL_SCRIPTS/run_agent.py" \
  "$SKILL_SCRIPTS/score.py"

echo "→ Packaging dist/ zips"
rm -rf "$DIST"; mkdir -p "$DIST"
( cd skill && zip -rq "../$DIST/upstage-studio.zip" upstage-studio \
    -x '*.DS_Store' '*__pycache__*' )
( cd gui && zip -q "../$DIST/upstage-batch-gui.zip" \
    upstage_batch_gui.py upstage_batch.py README.md )

echo "✓ Built:"
ls -la "$DIST"
