# Default to the oldest supported runtime; the workflow also tests pinned 24.04.
ARG BASE_IMAGE=ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
# These are test/display tools. Native viewer and authoring packages stay absent.
RUN apt-get update && apt-get install --no-install-recommends -y \
      dbus xvfb xdotool python3 procps coreutils util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY verify /opt/derivation-verify
WORKDIR /work
ENTRYPOINT ["python3", "-B", "/opt/derivation-verify/runtime.py"]
