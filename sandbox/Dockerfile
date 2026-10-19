FROM ubuntu:26.04

# Browser-free by default. Opt in at image build time; never download while coding.
ARG INSTALL_BROWSER_TESTS=false

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/home/ubuntu/.local/bin:/root/.local/bin:/workspace/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq make ripgrep tree coreutils sqlite3 \
    python3 python3-pip python3-venv nodejs npm && \
    pip3 install --break-system-packages --no-cache-dir uv autopep8 && \
    npm install --global bun && \
    rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/roai-browsers
RUN if [ "$INSTALL_BROWSER_TESTS" = "true" ]; then \
    npm install --prefix /opt/roai-browser --omit=dev --no-audit --no-fund playwright@1.64.1 && \
    node /opt/roai-browser/node_modules/playwright/cli.js install --with-deps --only-shell chromium && \
    chmod -R a+rX /opt/roai-browser /opt/roai-browsers && \
    rm -rf /var/lib/apt/lists/* /root/.npm; \
    fi
COPY helpers/roai-tree /usr/local/bin/roai-tree
COPY helpers/roai-read /usr/local/bin/roai-read
COPY helpers/roai-write /usr/local/bin/roai-write
COPY helpers/roai-project-info /usr/local/bin/roai-project-info
COPY helpers/roai-python-setup /usr/local/bin/roai-python-setup
COPY helpers/roai-node-setup /usr/local/bin/roai-node-setup
COPY helpers/roai-browser-test /usr/local/bin/roai-browser-test
RUN chmod +x /usr/local/bin/roai-*
ENV HOME=/home/ubuntu
WORKDIR /workspace
CMD ["sleep", "infinity"]
