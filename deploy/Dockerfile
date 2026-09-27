FROM python:3.12-slim


WORKDIR /app


RUN apt-get update && apt-get install -y \
# try fix with bash
    bash \
    openjdk-21-jdk-headless \
    wget \
    curl \
    git \
    tar \
    gzip \
    htop \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*


# JAVA_HOME must not hardcode the arch (java-21-openjdk-amd64 vs -arm64):
# symlink whatever apt installed to a stable path.
RUN ln -s /usr/lib/jvm/java-21-openjdk-* /usr/lib/jvm/jdk-21
ENV JAVA_HOME=/usr/lib/jvm/jdk-21
ENV PATH=$JAVA_HOME/bin:$PATH


# Download Isabelle BEFORE copying the source tree so this heavy, network-bound
# layer stays cached across code changes. Pick the tarball matching the BUILD
# architecture — running the x86-64 bundle under qemu on Apple Silicon made
# Isabelle 5-20x slower. isabelle.in.tum.de is sometimes unstable (its dist
# redirect target refuses connections), so retry hard and fall back to a mirror.
RUN mkdir -p /opt \
 && cd /opt \
 && case "$(uname -m)" in \
      aarch64|arm64) TARBALL=Isabelle2025-2_linux_arm.tar.gz ;; \
      *)             TARBALL=Isabelle2025-2_linux.tar.gz ;; \
    esac \
 && for url in \
      "https://isabelle.in.tum.de/dist/$TARBALL" \
      "https://mirror.clarkson.edu/isabelle/dist/$TARBALL" \
      "https://www.cl.cam.ac.uk/research/hvg/Isabelle/dist/$TARBALL" \
      "https://proofcraft.systems/isabelle/dist/$TARBALL" \
    ; do \
      wget --tries=2 --waitretry=5 --retry-connrefused "$url" && break ; \
    done \
 && test -s "$TARBALL" \
 && tar -xf "$TARBALL" \
 && mv Isabelle2025-2 isabelle \
 && rm -f "$TARBALL"

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

# Global Python deps (no venv) — copy only requirement.txt first so dependency
# installs are cached independently of source edits.
COPY requirement.txt /app/requirement.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirement.txt

COPY . /app/

# Windows 
RUN find repl -type f \( -name "*.sh" -o -name "init" -o -name "gradlew" \) -exec sed -i 's/\r$//' {} \; \
 && chmod +x repl/Admin/init repl/gradlew \
 && ./repl/Admin/init

# Linux/MacOS
#RUN chmod +x repl/Admin/init \
#    && ./repl/Admin/init \
#    && find /root/.isabelle/Isabelle2025-2/contrib -path "*/etc/settings" -type f -exec sed -i 's/\r$//' {} \;

RUN cd repl \
    && chmod +x gradlew \
    && ./gradlew build


CMD ["bash"] 