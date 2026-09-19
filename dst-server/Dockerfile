# DST dedicated server. Base image ships steamcmd with its i386 libraries and the
# non-root `steam` user (UID 1000). Pinned by digest; bump deliberately.
FROM docker.io/cm2network/steamcmd:latest@sha256:45f6515d6c4dcde659c9ad6872bdbeacd1bf5c4e7f241829c4d2f28fb5eda581

USER root
# libcurl3t64-gnutls: the DST binary dlopens libcurl-gnutls.so.4 (Debian 13 name).
# tini: PID 1 that forwards SIGTERM to the entrypoint. inotify-tools: cluster wait.
# zip: stop backups. procps/unzip: debugging convenience.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates libcurl3t64-gnutls tini inotify-tools procps zip unzip \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o steam -g steam \
      /opt/dst /data/klei/DoNotStarveTogether /data/mods /data/backups /ctl

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=0644 lib/dst.sh /usr/local/lib/dst/dst.sh

USER steam
WORKDIR /home/steam
ENV STEAMCMD=/home/steam/steamcmd/steamcmd.sh \
    DST_DIR=/opt/dst \
    KLEI_DIR=/data/klei \
    CTL_DIR=/ctl \
    BACKUP_DIR=/data/backups \
    LOCAL_MODS_DIR=/data/mods

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
