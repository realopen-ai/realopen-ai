FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/home/ubuntu/.local/bin:/root/.local/bin:/workspace/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq make ripgrep tree coreutils sqlite3 \
    python3 python3-pip python3-venv nodejs npm && \
    pip3 install --break-system-packages --no-cache-dir uv autopep8 && \
    npm install --global bun && \
    rm -rf /var/lib/apt/lists/*
COPY helpers/roai-tree /usr/local/bin/roai-tree
COPY helpers/roai-read /usr/local/bin/roai-read
COPY helpers/roai-write /usr/local/bin/roai-write
COPY helpers/roai-project-info /usr/local/bin/roai-project-info
COPY helpers/roai-python-setup /usr/local/bin/roai-python-setup
COPY helpers/roai-node-setup /usr/local/bin/roai-node-setup
RUN chmod +x /usr/local/bin/roai-*
ENV HOME=/home/ubuntu
WORKDIR /workspace
CMD ["sleep", "infinity"]
