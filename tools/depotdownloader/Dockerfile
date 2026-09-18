# Local-dev helper: DepotDownloader fetches the DST dedicated server (app 343050)
# with anonymous login. Needed on Apple Silicon because steamcmd (32-bit) cannot
# run under podman machine's qemu-i386. Not part of the VPS stack.
FROM mcr.microsoft.com/dotnet/runtime-deps:9.0
ARG DD_VERSION=3.4.0
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && case "$TARGETARCH" in amd64) a=x64 ;; arm64) a=arm64 ;; *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; esac \
 && curl -fsSL -o /tmp/dd.zip "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${DD_VERSION}/DepotDownloader-linux-${a}.zip" \
 && mkdir -p /opt/depotdownloader \
 && unzip -q /tmp/dd.zip -d /opt/depotdownloader \
 && chmod +x /opt/depotdownloader/DepotDownloader \
 && rm /tmp/dd.zip \
 && useradd -u 1000 -m steam \
 && install -d -o steam -g steam /opt/dst
USER steam
ENTRYPOINT ["/opt/depotdownloader/DepotDownloader"]
