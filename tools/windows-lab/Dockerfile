FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        novnc \
        ovmf \
        qemu-system-x86 \
        qemu-utils \
        socat \
        swtpm \
        tini \
        websockify \
        xorriso \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/specbutler-windows-entrypoint
RUN chmod 0755 /usr/local/bin/specbutler-windows-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/specbutler-windows-entrypoint"]
