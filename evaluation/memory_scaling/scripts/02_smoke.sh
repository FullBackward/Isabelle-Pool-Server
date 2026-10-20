#!/usr/bin/env bash
# Pipeline smoke: k = 1 and 2 on HOL for the three topologies with short phases (~10 min).
# Must pass before any experiment. Prints a wall-time estimate of the full E1 matrix from
# the smoke's own cell times. Env: OUT (default results/smoke), IPS_EXTERNAL=1 to use a
# running IPS at IPS_URL instead of starting one (the local container case).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
OUT="${OUT:-$HERE/results/smoke}"
EXTRA=()
[ "${IPS_EXTERNAL:-0}" = "1" ] && EXTRA=(--ips-external --ips-url "${IPS_URL:-http://127.0.0.1:8000}")
rc=0; declare -A T
for tool in ips pide-a pide-b; do
  for k in 1 2; do
    t0=$(date +%s)
    python -u -m memscale.run_cell --tool "$tool" --heap HOL --k "$k" --rep 0 --out "$OUT" --baseline-s 5 --idle-s 10 --force "${EXTRA[@]}" 2>&1 | grep -E 'phase|started in|loaded in|re-check|outcome|ERROR'
    T["$tool-$k"]=$(( $(date +%s) - t0 ))
    m="$OUT/$tool/HOL/k$k/rep0/manifest.json"
    o=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['outcome'])" "$m" 2>/dev/null || echo missing)
    echo "== $tool k=$k: outcome $o in ${T[$tool-$k]}s"; [ "$o" = "ok" ] || rc=1
  done
done
python - "$HERE/config.yaml" "${T[ips-2]:-0}" "${T[pide-a-2]:-0}" "${T[pide-b-2]:-0}" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])); t = [float(x) for x in sys.argv[2:5]]
ph = cfg["phases"]; fixed = ph["baseline_s"] + 2 * ph["idle_s"] + ph["idle_s"] + 10 - (5 + 3 * 10 + 10)  # vs the smoke's 5/10 s phases
total = 0.0
for ti, tool in enumerate(("ips", "pide-a", "pide-b")):
    per_session = max(1.0, (t[ti] - 45) / 2)  # smoke k=2: minus its phases, per session start+load+recheck
    for heap, hc in cfg["heaps"].items():
        for k in hc["ks"]:
            reps = cfg.get("repeats_large_k", 1) if k >= cfg.get("large_k", 16) else cfg.get("repeats", 3)
            total += reps * (fixed + 5 + 3 * ph["idle_s"] + 10 + k * per_session * (3 if heap == "HOL-Analysis" else 1.5 if heap == "HOL-Library" else 1))
print(f"estimated E1 matrix wall time from this smoke: ~{total/3600:.1f} h (HOL-based per-session costs x1.5 for HOL-Library, x3 for HOL-Analysis; cold starts of large heaps may add more)")
PY
exit $rc
