# Native dependencies are built once per Dockerfile/base-image change.
ARG BASE_IMAGE=ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.31" \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
      python3 build-essential cmake ninja-build valac pkg-config git ca-certificates curl \
      file desktop-file-utils patchelf binutils xz-utils squashfs-tools \
      libgtk-3-dev libgee-0.8-dev libpoppler-glib-dev libjson-glib-dev \
      librsvg2-dev libgirepository1.0-dev libgtk-3-bin libglib2.0-bin \
      fonts-dejavu-core adwaita-icon-theme hicolor-icon-theme shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
