# Image for colleagues' containers on a work server (「工作」menu): git, the agent CLIs and optional toolchains.
# Build: docker build -t agent-hub-work:dev deploy/workhost
#   --build-arg WITH_JAVA=0     skip Java 17 + Maven
#   --build-arg WITH_GO=1       add Go
#   --build-arg WITH_CLAUDE=0   skip the Claude Code CLI
# Dependency caches (~/.m2, ~/.npm, ~/go) live on each colleague's own volume, so they survive a new container.
ARG BASE=node:22-bookworm-slim
FROM ${BASE}

ARG CODEX_VERSION=0.166.0
ARG CLAUDE_VERSION=latest
ARG GO_VERSION=1.24.4
# Maven 3.9+ reads a lent read-only local repository as a "tail" (connector: workhost maven); Debian ships 3.8
ARG MAVEN_VERSION=3.9.9
ARG WITH_JAVA=1
ARG WITH_GO=0
ARG WITH_CLAUDE=1
RUN set -eux; \
    pkgs="git ca-certificates procps openssh-client python3 python3-pip python3-venv curl"; \
    if [ "$WITH_JAVA" = 1 ]; then pkgs="$pkgs openjdk-17-jdk-headless"; fi; \
    apt-get update; apt-get install -y --no-install-recommends $pkgs; rm -rf /var/lib/apt/lists/*; \
    if [ "$WITH_JAVA" = 1 ]; then \
      curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" | tar -C /opt -xz; \
      ln -s /opt/apache-maven-${MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn; \
    fi; \
    if [ "$WITH_GO" = 1 ]; then \
      arch=$(dpkg --print-architecture); \
      curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" | tar -C /usr/local -xz; \
      ln -s /usr/local/go/bin/go /usr/local/bin/go; ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt; \
    fi; \
    npm install -g @openai/codex@${CODEX_VERSION}; \
    if [ "$WITH_CLAUDE" = 1 ]; then npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION}; fi; \
    npm cache clean --force; \
    useradd -m -u 1001 -s /bin/bash dev

USER dev
WORKDIR /home/dev
ENV PATH=/home/dev/go/bin:$PATH
RUN git config --global init.defaultBranch main && git config --global safe.directory '*'
