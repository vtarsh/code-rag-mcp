#!/usr/bin/env bash
# docs_validate_all.sh — run all 5 doc validators, log results to /tmp/docs-drift.log.
# Designed for launchd daily run + Claude Code Stop hook.
# Exit 0 on clean. Exit 1 on any validator failure.

set -u
LOG=/tmp/docs-drift.log
SCRIPTS=~/.code-rag-mcp/scripts
PY=/usr/local/bin/python3.12

ts() { date +"%Y-%m-%d %H:%M:%S"; }

{
  echo "=== docs validate run @ $(ts) ==="

  fail=0
  for v in validate_doc_file_line_refs validate_doc_anchors validate_doc_related_repos validate_doc_size validate_doc_frontmatter validate_overlay_vs_proto; do
    echo
    echo "-- $v --"
    if "$PY" "$SCRIPTS/$v.py"; then
      :
    else
      fail=1
    fi
  done

  echo
  echo "-- generate_housekeeping_report (H10 implementation) --"
  if "$PY" "$SCRIPTS/generate_housekeeping_report.py"; then
    :
  else
    fail=1
  fi

  echo
  if [ "$fail" -eq 0 ]; then
    echo "RESULT: all clean"
  else
    echo "RESULT: drift detected ($(date +%H:%M))"
  fi
} >>"$LOG" 2>&1

exit "$fail"
