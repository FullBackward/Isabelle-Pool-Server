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
#  - No system JDK and NEVER a JAVA_HOME: Isabelle 2026 resolves its own
#    bundled JDK (a foreign JAVA_HOME fails with "Unknown JAVA_HOME") and
#    compiles the Scala backend itself (`isabelle scala_build`, from
#    server/repl/etc/build.props). server/repl/build.gradle is IDE-only.
#  - Pin HOME=/root: Docker Desktop's BuildKit may run steps with the host
#    user's HOME, which makes Isabelle resolve an empty ISABELLE_HOME_USER.

FROM python:3.12-slim

# Default version = ISABELLE_VERSION in .env.example; docker-compose passes the
# value from .env as this build arg, deploy/native_setup.sh reads the same key.
ARG ISABELLE_VERSION=Isabelle2026-RC2
ARG GIT_REVISION=unknown

LABEL org.opencontainers.image.title="Isabelle Pool Server" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      isabelle-pool-server.isabelle_version="${ISABELLE_VERSION}"

ENV HOME=/root
WORKDIR /app

# System packages: ONE list shared with the native install
# (deploy/apt_packages.txt — fontconfig + a font are required even headless:
# Isabelle 2026's Build.build_store calls Isabelle_Fonts.init() on every
# session start and the JVM dies with "Fontconfig head is null" without them;
# --no-install-recommends would drop them, hence the explicit list).
COPY deploy/apt_packages.txt /tmp/apt_packages.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends $(sed -e 's/#.*//' /tmp/apt_packages.txt) \
 && rm -rf /var/lib/apt/lists/* /tmp/apt_packages.txt

# Download Isabelle BEFORE copying the source tree so this heavy, network-bound
# layer stays cached across code changes. Mirrors (and the speed-floor rule)
# come from deploy/isabelle_mirrors.txt, shared with the native install. Pick
# the tarball matching the BUILD architecture — running the x86-64 bundle under
# qemu on Apple Silicon made Isabelle 5-20x slower.
COPY deploy/isabelle_mirrors.txt /tmp/isabelle_mirrors.txt
RUN mkdir -p /opt \
 && cd /opt \
 && case "$(uname -m)" in \
      aarch64|arm64) TARBALL="${ISABELLE_VERSION}_linux_arm.tar.gz" ;; \
      *)             TARBALL="${ISABELLE_VERSION}_linux.tar.gz" ;; \
    esac \
 && for base in $(sed -e 's/#.*//' -e "s/VERSION/${ISABELLE_VERSION}/g" /tmp/isabelle_mirrors.txt); do \
      url="${base}/${TARBALL}"; \
      rm -f "$TARBALL"; \
      echo "==> trying $url"; \
      curl -fL --retry 2 --retry-connrefused --connect-timeout 20 \
           --speed-limit 1000000 --speed-time 30 -o "$TARBALL" "$url" && break ; \
    done \
 && test -s "$TARBALL" \
 && tar -xf "$TARBALL" \
 && mv "${ISABELLE_VERSION}" isabelle \
 && rm -f "$TARBALL" /tmp/isabelle_mirrors.txt

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

# Python deps first (cached independently of source edits). pyproject.toml is
# the single dependency list: install its runtime deps plus the `mcp` extra
# (mcp<2: mcp 2.0 removed mcp.server.fastmcp); the dev/eval extras stay out of
# the image. Extracted with tomllib so this layer depends on pyproject.toml only.
COPY pyproject.toml /app/pyproject.toml
RUN python -m pip install --upgrade pip \
 && python -c "import tomllib; p = tomllib.load(open('/app/pyproject.toml', 'rb'))['project']; [print(d) for d in p['dependencies'] + p['optional-dependencies']['mcp']]" > /tmp/deps.txt \
 && python -m pip install -r /tmp/deps.txt && rm /tmp/deps.txt

COPY . /app/

# Normalise line endings for scripts that may come from a Windows checkout,
# register the REPL component (+ py4j/spliff contribs) with Isabelle, then let
# Isabelle compile the Scala backend (server/repl/lib/repl.jar) with its own
# bundled JDK — the same build `isabelle scala` would otherwise run lazily at
# the first gateway start, done here so compile errors fail the image build.
RUN find server/repl -type f \( -name "*.sh" -o -name "init" \) -exec sed -i 's/\r$//' {} \; \
 && chmod +x server/repl/Admin/init server/repl/Admin/*.sh \
 && ./server/repl/Admin/init \
 && isabelle scala_build

EXPOSE 8000

# Entrypoint: component re-registration into a (possibly stale) user volume →
# ML heap cap in the user settings → foreground API server. docker-compose
# sets the same command; `docker run` gets it by default.
CMD ["bash", "./server/repl/Admin/container_entrypoint.sh"]
