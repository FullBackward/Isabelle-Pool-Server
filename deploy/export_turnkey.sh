#!/usr/bin/env bash
#
# Build a self-contained "turnkey" image from the RUNNING deployment: the
# current image plus the session heaps and Isabelle user settings held in the
# isabelle_user_data volume. Recipients `docker load` and `docker run` it — no
# build, no volume, heaps included (e.g. to reproduce published results).
#
# Run from the repo root with the server container up; prebuild the heaps you
# want shipped first (./deploy/setup.sh --build-heaps "HOL-Library HOL-Analysis").
#
#   ./deploy/export_turnkey.sh                       # -> image isabelle-pool-server:turnkey
#   ./deploy/export_turnkey.sh my/image:tag --save   # ...and `docker save | gzip` it into the repo root
#
# Env overrides: ISABELLE_CONTAINER (default isabelle-pool-server),
# BASE_IMAGE (default isabelle-pool-server:local), EXPORT_STAGING_DIR
# (default: a temp dir; heaps can be several GB, so point it at a big disk).

set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-isabelle-pool-server:turnkey}"
SAVE=0; [[ "${2:-}" == "--save" ]] && SAVE=1
CONTAINER="${ISABELLE_CONTAINER:-isabelle-pool-server}"
BASE_IMAGE="${BASE_IMAGE:-isabelle-pool-server:local}"

docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || { echo "container '$CONTAINER' not found — start it first (docker compose up -d isabelle-pool-server)" >&2; exit 1; }

STAGING="${EXPORT_STAGING_DIR:-$(mktemp -d)}"
[[ -n "${EXPORT_STAGING_DIR:-}" ]] || trap 'rm -rf "$STAGING"' EXIT
HOME_OUT="$STAGING/export_isabelle_home"
rm -rf "$HOME_OUT"

ID="$(docker exec "$CONTAINER" isabelle getenv -b ISABELLE_IDENTIFIER)"
echo "==> exporting heaps + settings of $ID from $CONTAINER"
mkdir -p "$HOME_OUT/$ID"
docker cp "$CONTAINER:/root/.isabelle/$ID/etc" "$HOME_OUT/$ID/"       # settings + component registration
# user-built heaps (HOL-Library, HOL-Analysis, ...); absent until something was built
docker cp "$CONTAINER:/root/.isabelle/$ID/heaps" "$HOME_OUT/$ID/" 2>/dev/null \
  || echo "    (no user-built heaps in the volume yet — only the distribution heaps ship; see setup.sh --build-heaps)"
# heap-pool manifests/projects (POST /api/v1/heaps), if any were built
docker cp "$CONTAINER:/root/.isabelle/heap_pool" "$HOME_OUT/" 2>/dev/null || true
du -sh "$HOME_OUT" | sed 's/^/    /'

REV="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo "==> building $TAG (FROM $BASE_IMAGE, revision $REV)"
docker build -f deploy/Dockerfile.export \
  --build-arg BASE_IMAGE="$BASE_IMAGE" --build-arg GIT_REVISION="$REV" \
  -t "$TAG" "$STAGING"

if [[ "$SAVE" -eq 1 ]]; then
  OUT="$(echo "$TAG" | tr '/:' '--').tar.gz"
  echo "==> saving $OUT (large; a few minutes)"
  docker save "$TAG" | gzip > "$OUT"
  echo "    $(du -h "$OUT" | cut -f1)  $OUT"
fi

cat <<EOT

Turnkey image built: $TAG  (source commit $REV, in the image label
org.opencontainers.image.revision). Recipient runbook:

  docker load < <file>.tar.gz                      # if shipped as a tarball
  docker run -d --name isabelle-pool-server -p 8000:8000 --memory 14g \\
    -e ISABELLE_ADMIN_TOKEN=\$(openssl rand -hex 16) $TAG
  curl http://localhost:8000/healthz               # 1-2 min for the gateway JVM
  curl http://localhost:8000/api/v1/heaps/available

Optional: -v isabelle_user_data:/root/.isabelle keeps new heaps across container
replacement; -e ISABELLE_ML_MAXHEAP_MB=<MB> resizes the per-process ML cap.
EOT
