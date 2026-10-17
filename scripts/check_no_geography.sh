#!/usr/bin/env bash
# Fail if the given source tree (default: src/) contains hard-coded geography:
#   - decimal literals that look like latitude/longitude (4+ decimal places)
#   - known site identifiers from the reference profile
# Geography belongs in profiles/<slug>/profile.yaml, never in code.
set -euo pipefail

target="${1:-src}"
coord_re='-?[0-9]{1,3}\.[0-9]{4,}'
id_re='MA00123|6116810|01109570|01090003000173|MA51027|USW00094746'

status=0
if grep -rnE --include='*.py' -e "$coord_re" "$target"; then
  echo "ERROR: lat/lon-looking literal found in $target (move it to a region profile)" >&2
  status=1
fi
if grep -rnE --include='*.py' -e "$id_re" "$target"; then
  echo "ERROR: known site identifier found in $target (move it to a region profile)" >&2
  status=1
fi
if [ "$status" -eq 0 ]; then
  echo "OK: no hard-coded geography in $target"
fi
exit "$status"
