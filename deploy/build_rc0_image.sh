#!/usr/bin/env bash
#
# Build the Isabelle2026-RC0 gym image via the VALIDATED scripted-assembly
# path (docker cp into a base container + docker commit). This is what
# Dockerfile.rc0 documents declaratively; the declarative BuildKit path is
# currently blocked by the "Unknown JAVA_HOME" environment quirk, so this
# script is the canonical RC0 image build.
#
# Usage:
#   ./deploy/build_rc0_image.sh [staging-dir] [tag]
#     staging-dir  default ~/isabelle2026-build — must contain:
#                    isabelle/      prebuilt RC0 Isabelle tree
#                    isabelle_user/ installed components (contrib/, etc/)
#                    app/           repo content (git archive of 2026-RC0)
#     tag          default isabellegym-isabelle-gym:2026rc0-clean
#
# Base: the LOCAL 2025-2 gym image (python3.12, openjdk-21, app deps).

set -euo pipefail

S="${1:-$HOME/isabelle2026-build}"
TAG="${2:-isabellegym-isabelle-gym:2026rc0-clean}"
NAME="rc0-assembly"

for d in isabelle isabelle_user app; do
  [[ -d "$S/$d" ]] || { echo "staging dir missing $S/$d" >&2; exit 1; }
done

docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" \
  -e HOME=/root \
  -e ISABELLE_HOME=/opt/isabelle \
  -e JAVA_HOME=/root/.isabelle/contrib/jdk-25.0.4/x86_64-linux \
  isabellegym-isabelle-gym:latest sleep infinity

echo "==> replacing /opt/isabelle, /root/.isabelle, /app with RC0 content"
docker exec "$NAME" rm -rf /opt/isabelle /root/.isabelle /app
docker cp "$S/isabelle" "$NAME":/opt/isabelle
docker cp "$S/isabelle_user" "$NAME":/root/.isabelle
docker cp "$S/app" "$NAME":/app

echo "==> pip install mcp<2 (mcp 2.0 removed FastMCP)"
docker exec "$NAME" python -m pip install "mcp<2"

echo "==> component registration (repl/Admin/init)"
docker exec "$NAME" bash -c "cd /app \
  && find repl -type f \( -name '*.sh' -o -name 'init' -o -name 'gradlew' \) -exec sed -i 's/\r$//' {} \; \
  && chmod +x repl/Admin/init repl/gradlew \
  && ./repl/Admin/init"

echo "==> Scala backend build (gradle; JDK scoped to system java-21 for this step)"
docker exec "$NAME" bash -c "cd /app/repl \
  && chmod +x gradlew \
  && JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 ./gradlew build"

echo "==> commit as $TAG"
docker commit -c 'CMD ["bash"]' "$NAME" "$TAG"
docker rm -f "$NAME" >/dev/null
echo "==> done: $TAG"
