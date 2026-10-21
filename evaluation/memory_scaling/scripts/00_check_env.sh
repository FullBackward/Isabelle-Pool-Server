#!/usr/bin/env bash
# Environment check for the memory-scaling experiments (run on the server, from the repo root
# or from evaluation/memory_scaling). Prints the Appendix D block and fails on a missing piece.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
REPO="$(cd "$HERE/../.." && pwd)"
ok=0; bad=0
pass() { echo "  PASS $1"; ok=$((ok+1)); }
fail() { echo "  FAIL $1"; bad=$((bad+1)); }
echo "== memory-scaling environment check ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "host: $(hostname) kernel: $(uname -r) arch: $(uname -m) cpus: $(nproc 2>/dev/null || sysctl -n hw.ncpu)"
grep -E 'MemTotal' /proc/meminfo 2>/dev/null || echo "MemTotal: (no /proc/meminfo — not Linux?)"
if command -v isabelle >/dev/null 2>&1; then
  pass "isabelle on PATH: $(command -v isabelle) ($(isabelle version 2>/dev/null) id $(isabelle version -i 2>/dev/null))"
else fail "isabelle not on PATH (ISABELLE_HOME/bin)"; fi
EXPECTED=$(grep -E '^\s*expected_version:' "$HERE/config.yaml" | awk '{print $2}')
[ "$(isabelle version 2>/dev/null)" = "$EXPECTED" ] && pass "Isabelle version is $EXPECTED" || fail "Isabelle version $(isabelle version 2>/dev/null) != config expected_version $EXPECTED"
echo "  ML_OPTIONS=$(isabelle getenv -b ML_OPTIONS 2>/dev/null)"
echo "  ISABELLE_TOOL_JAVA_OPTIONS=$(isabelle getenv -b ISABELLE_TOOL_JAVA_OPTIONS 2>/dev/null)"
echo "  ISABELLE_HOME_USER=$(isabelle getenv -b ISABELLE_HOME_USER 2>/dev/null)"
isabelle getenv -b ML_OPTIONS 2>/dev/null | grep -q -- "--maxheap" && pass "ML heap cap present (applies to both tools)" || fail "no --maxheap in ML_OPTIONS (run server/repl/Admin/ensure_settings.sh)"
# both components
# `isabelle pide_mcp -?` prints its usage and exits non-zero, so test the text (this also builds the jar once)
if isabelle pide_mcp -? 2>&1 | grep -q "Usage: isabelle pide_mcp"; then pass "PIDE MCP component (isabelle pide_mcp)"; else fail "isabelle pide_mcp not available (isabelle components -u <isabelle-pide-mcp>)"; fi
if isabelle components -l 2>/dev/null | grep -q "repl"; then pass "IPS repl component registered"; else echo "  NOTE IPS repl component not listed by 'isabelle components -l' (server/repl/Admin/init registers it at server start)"; fi
# heaps
HEAPS_DIR="$(isabelle getenv -b ISABELLE_HEAPS 2>/dev/null)"
for h in $(grep -E '^\s{2}[A-Za-z-]+:\s*\{ks' "$HERE/config.yaml" | sed -E 's/^\s*([A-Za-z-]+):.*/\1/'); do
  if ls "$HEAPS_DIR"/*/"$h" >/dev/null 2>&1 || ls "$(isabelle getenv -b ISABELLE_HOME 2>/dev/null)"/heaps/*/"$h" >/dev/null 2>&1; then pass "heap $h present"; else fail "heap $h missing (scripts/01_build_heaps.sh)"; fi
done
# python side
"$PY" -c "import yaml, httpx, client.async_client" 2>/dev/null && pass "python deps (pyyaml, httpx, client)" || fail "python deps: pip install -e '.[eval]' -e ./client (PYTHON=$PYTHON)"
# kernel facts
[ -r /proc/self/smaps_rollup ] && pass "smaps_rollup readable" || fail "no /proc/self/smaps_rollup (kernel too old?)"
[ -f /sys/fs/cgroup/cgroup.controllers ] && pass "cgroup v2" || fail "cgroup v2 not mounted at /sys/fs/cgroup"
if command -v systemd-run >/dev/null 2>&1; then
  if systemd-run --user --scope --quiet true 2>/dev/null; then pass "systemd-run --user --scope works (IPS runs in its own scope)"; else fail "systemd-run --user --scope failed: enable a user manager (loginctl enable-linger \$USER; XDG_RUNTIME_DIR) — E1/E3 need it"; fi
else fail "systemd-run not found — IPS cannot run in its own scope (its memory gate would count the competitor); install systemd or run E1 in Docker"; fi
echo "repo: $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null) ($(git -C "$REPO" branch --show-current 2>/dev/null))"
echo "== $ok passed, $bad failed"
[ "$bad" -eq 0 ]
