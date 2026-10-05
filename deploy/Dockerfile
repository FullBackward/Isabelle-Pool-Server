# Isabelle Pool Server image — ONE Dockerfile for every Isabelle version.
#
#   docker compose build isabelle-pool-server                                  # default version below
#   docker build -f deploy/Dockerfile --build-arg ISABELLE_VERSION=Isabelle2026-RC2 -t isabelle-pool-server:2026rc2 .
#
# ISABELLE_VERSION is the distribution name (Isabelle2026-RC2, Isabelle2026,
# Isabelle2025-2 …). Release candidates live in a per-RC website directory
# (website-<version>/dist/), final releases in dist/; both are tried, on the
# official host and on mirrors. Build context is the repo root.
#
# Isabelle 2026 notes (from the RC0 migration, docs/ISSUES.md / AGENTS.md):
#  - NEVER export a system JAVA_HOME globally — Isabelle 2026 resolves its own
#    bundled JDK and a foreign JAVA_HOME fails with "Unknown JAVA_HOME". The
#    system JDK is only needed by Gradle and is scoped to that one RUN step.
#  - Pin HOME=/root: Docker Desktop's BuildKit may run steps with the host
#    user's HOME, which makes Isabelle resolve an empty ISABELLE_HOME_USER.

FROM python:3.12-slim

ARG ISABELLE_VERSION=Isabelle2026-RC2
# Space-separated base URLs; "<base>/<tarball>" is tried in order. Any mirror
# under 1 MB/s for 30 s is abandoned (curl --speed-limit/--speed-time) — a
# merely SLOW mirror never "fails", so a plain fallback chain would sit on it
# for hours (2026-09-27: dist.isabelle.cit.tum.de trickled at 150 KB/s).
ARG ISABELLE_DIST_URLS="https://isabelle.in.tum.de/website-${ISABELLE_VERSION}/dist \
    https://isabelle.sketis.net/website-${ISABELLE_VERSION}/dist \
    https://www.cl.cam.ac.uk/research/hvg/Isabelle/dist \
    https://proofcraft.systems/isabelle/dist \
    https://isabelle.in.tum.de/dist \
    https://mirror.clarkson.edu/isabelle/dist"
ARG GIT_REVISION=unknown

LABEL org.opencontainers.image.title="Isabelle Pool Server" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      isabelle-pool-server.isabelle_version="${ISABELLE_VERSION}"

ENV HOME=/root
WORKDIR /app

# fontconfig + one font family are REQUIRED even though the JVM runs headless:
# Isabelle 2026's Build.build_store (Pure/Build/build.scala) calls
# Isabelle_Fonts.init() -> GraphicsEnvironment.getLocalGraphicsEnvironment()
# on every `isabelle build`, i.e. on every session start. Without them the JVM
# dies with "Fontconfig head is null" and no session can be created.
# (--no-install-recommends drops them; the old Dockerfile got them implicitly
# as recommends of openjdk-21-jdk-headless.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash openjdk-21-jdk-headless curl git tar gzip ca-certificates procps \
    fontconfig fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Download Isabelle BEFORE copying the source tree so this heavy, network-bound
# layer stays cached across code changes. Pick the tarball matching the BUILD
# architecture — running the x86-64 bundle under qemu on Apple Silicon made
# Isabelle 5-20x slower.
RUN mkdir -p /opt \
 && cd /opt \
 && case "$(uname -m)" in \
      aarch64|arm64) TARBALL="${ISABELLE_VERSION}_linux_arm.tar.gz" ;; \
      *)             TARBALL="${ISABELLE_VERSION}_linux.tar.gz" ;; \
    esac \
 && for base in ${ISABELLE_DIST_URLS}; do \
      url="${base}/${TARBALL}"; \
      rm -f "$TARBALL"; \
      echo "==> trying $url"; \
      curl -fL --retry 2 --retry-connrefused --connect-timeout 20 \
           --speed-limit 1000000 --speed-time 30 -o "$TARBALL" "$url" && break ; \
    done \
 && test -s "$TARBALL" \
 && tar -xf "$TARBALL" \
 && mv "${ISABELLE_VERSION}" isabelle \
 && rm -f "$TARBALL"

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

# Python deps first (cached independently of source edits). The MCP SDK is
# pinned <2 (mcp 2.0 removed mcp.server.fastmcp).
COPY requirement.txt /app/requirement.txt
COPY mcp_servers/requirements.txt /app/mcp_servers/requirements.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirement.txt -r /app/mcp_servers/requirements.txt

COPY . /app/

# Normalise line endings for scripts that may come from a Windows checkout,
# register the REPL component (+ py4j/spliff contribs) with Isabelle, and
# build the Scala backend. Gradle gets the system JDK via a JAVA_HOME scoped
# to its own step; Isabelle's own `isabelle scala` uses the bundled JDK.
RUN find server/repl -type f \( -name "*.sh" -o -name "init" -o -name "gradlew" \) -exec sed -i 's/\r$//' {} \; \
 && chmod +x server/repl/Admin/init server/repl/Admin/*.sh server/repl/gradlew \
 && ./server/repl/Admin/init

RUN cd server/repl \
 && JAVA_HOME="$(ls -d /usr/lib/jvm/java-21-openjdk-* | head -1)" ./gradlew build --no-daemon

EXPOSE 8000

# Entrypoint: component re-registration into a (possibly stale) user volume →
# ML heap cap in the user settings → foreground API server. docker-compose
# sets the same command; `docker run` gets it by default.
CMD ["bash", "./server/repl/Admin/container_entrypoint.sh"]
