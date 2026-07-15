#!/usr/bin/env bash
# build.sh — assemble the two delivery artifacts from a single source of truth.
#
# The batch CLI (core/upstage_batch.py) is the ONE canonical copy. This script
# stamps it into both the skill and the GUI so each delivered artifact is fully
# self-contained, while you only ever edit the one file in core/. Run it after
# editing anything, before shipping.
#
# Outputs (in dist/):
#   upstage-studio.zip      — the Claude skill (technical / AI-native users)
#   upstage-batch-gui.zip   — the batch CLI + web GUI (non-technical users)
set -euo pipefail
cd "$(dirname "$0")"

CORE="core/upstage_batch.py"
SKILL_SCRIPTS="skill/upstage-studio/scripts"
DIST="dist"

echo "→ Stamping canonical batch CLI into both artifacts"
cp "$CORE" "$SKILL_SCRIPTS/upstage_batch.py"
cp "$CORE" "gui/upstage_batch.py"

echo "→ Cleaning build cruft"
find skill gui -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find skill gui -name '.DS_Store' -delete 2>/dev/null || true

echo "→ Sanity: compile every script"
python3 -m py_compile \
  "$CORE" \
  gui/upstage_batch_gui.py \
  "$SKILL_SCRIPTS/run_agent.py" \
  "$SKILL_SCRIPTS/score.py" \
  "$SKILL_SCRIPTS/upstage_batch.py"

echo "→ Packaging dist/ zips"
rm -rf "$DIST"; mkdir -p "$DIST"
( cd skill && zip -rq "../$DIST/upstage-studio.zip" upstage-studio \
    -x '*.DS_Store' '*__pycache__*' )
( cd gui && zip -q "../$DIST/upstage-batch-gui.zip" \
    upstage_batch_gui.py upstage_batch.py README.md )

echo "✓ Built:"
ls -la "$DIST"
