# Default to the oldest supported runtime; the workflow also tests pinned 24.04.
ARG BASE_IMAGE=ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
# These are test/display tools. Native viewer and authoring packages stay absent.
RUN apt-get update && apt-get install --no-install-recommends -y \
      dbus xvfb xdotool python3 procps coreutils util-linux \
    && rm -rf /var/lib/apt/lists/*
ARG TEST_UID=1000
ARG TEST_GID=1000
# D-Bus needs a passwd entry, not just Docker's numeric --user override.
# Ubuntu 24.04 already has UID/GID 1000; reuse it when it matches the runner.
RUN set -eu; \
    test "$TEST_UID" -gt 0; test "$TEST_GID" -gt 0; \
    if ! getent group "$TEST_GID" >/dev/null; then \
      groupadd --gid "$TEST_GID" derivation-test; \
    fi; \
    if getent passwd "$TEST_UID" >/dev/null; then \
      account="$(getent passwd "$TEST_UID" | cut -d: -f1)"; \
      usermod --gid "$TEST_GID" --home /home/derivation-test "$account"; \
    else \
      useradd --uid "$TEST_UID" --gid "$TEST_GID" --no-create-home \
        --home-dir /home/derivation-test derivation-test; \
    fi; \
    install -d -o "$TEST_UID" -g "$TEST_GID" /home/derivation-test /work
ENV HOME=/home/derivation-test
COPY verify /opt/derivation-verify
USER ${TEST_UID}:${TEST_GID}
WORKDIR /work
ENTRYPOINT ["python3", "-B", "/opt/derivation-verify/runtime.py"]
