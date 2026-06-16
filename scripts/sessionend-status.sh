#!/bin/bash
# Quick check: recent idle fires + extraction results
echo "── WX idle fires ──"
tail -5 ~/.config/synapse-wx/session_audit.log 2>/dev/null || echo "(no log)"
echo ""
echo "── TG idle fires ──"
tail -5 ~/.config/synapse-tg/session_audit.log 2>/dev/null || echo "(no log)"
echo ""
echo "── DB extraction results (last 10) ──"
sqlite3 ~/.config/marrow/marrow.db \
  "SELECT substr(target_id,1,8) as sid, summary, occurred_at FROM audit_log WHERE action='sessionend_extract' AND summary != 'start' ORDER BY id DESC LIMIT 10;" 2>/dev/null
echo ""
echo "── Errors ──"
for f in ~/Library/Logs/synapse-wx-sessionend.err.log ~/Library/Logs/synapse-tg-sessionend.err.log; do
  sz=$(stat -f%z "$f" 2>/dev/null || echo 0)
  if [ "$sz" -gt 0 ]; then echo "$(basename $f):"; tail -5 "$f"; fi
done
echo "(empty = no errors)"
