#!/usr/bin/env bash
# Pack results/ and attach the archive to a GitHub Release of this repository (an asset may be up
# to 2 GB; the git history stays small). Needs a token with "Contents: read and write" in GH_TOKEN,
# or one saved by `git config credential.helper store` (~/.git-credentials). No `gh` needed.
#   GH_TOKEN=... scripts/30_release_results.sh            # tag memscale-<UTC date>, archive in $HOME
#   TAG=trainmetrics-2026-10-25b scripts/30_release_results.sh
#   DRY_RUN=1 scripts/30_release_results.sh               # pack and print, no upload
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
STAMP="$(date -u +%Y-%m-%d)"
TAG="${TAG:-trainmetrics-$STAMP}"
FILE="${FILE:-$HOME/training-metrics-results-$STAMP.tgz}"
REPO="${REPO:-$(git -C "$HERE" remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')}"
if [ -z "${GH_TOKEN:-}" ] && [ -r "$HOME/.git-credentials" ]; then
  GH_TOKEN="$(grep -o 'github.com' -m1 "$HOME/.git-credentials" >/dev/null && sed -nE 's#https://[^:]+:([^@]+)@github.com.*#\1#p' "$HOME/.git-credentials" | head -1)"
fi
[ -n "${GH_TOKEN:-}" ] || { echo "GH_TOKEN is not set and ~/.git-credentials has no github.com token"; exit 2; }
export GH_TOKEN TAG FILE REPO
echo "packing results/ -> $FILE"
tar czf "$FILE" results/
ls -lh "$FILE"
[ -n "${DRY_RUN:-}" ] && { echo "DRY_RUN: would create release $TAG on $REPO and upload $(basename "$FILE")"; exit 0; }
python3 - <<'PY'
import json, os, subprocess, urllib.error, urllib.request
token, repo, tag, path = (os.environ[k] for k in ("GH_TOKEN", "REPO", "TAG", "FILE"))
commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
hdr = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
def call(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={**hdr, "Content-Type": ctype})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{url}: HTTP {exc.code}: {exc.read().decode()[:300]}") from exc
body = json.dumps({"tag_name": tag, "name": f"Training metrics results {tag}",
                   "body": f"training-metrics results archive; commit {commit}"}).encode()
rel = call(f"https://api.github.com/repos/{repo}/releases", body, "application/json")
print("release:", rel["html_url"])
with open(path, "rb") as fh:
    data = fh.read()
name = os.path.basename(path)
asset = call(rel["upload_url"].split("{")[0] + "?name=" + name, data, "application/gzip")
print("download:", asset["browser_download_url"])
PY
