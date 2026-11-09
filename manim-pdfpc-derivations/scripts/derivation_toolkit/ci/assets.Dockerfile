# Build with the derivation_toolkit directory as context.
ARG BASE_IMAGE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
      texlive-latex-extra texlive-fonts-recommended texlive-pictures tex-gyre \
      lmodern preview-latex-style dvisvgm poppler-utils inkscape \
      python3-pygments python3-pypdf python3-venv python3-dev \
      build-essential pkg-config git libcairo2-dev libpango1.0-dev \
    && rm -rf /var/lib/apt/lists/*
COPY animation/requirements.lock animation/requirements-test.lock /opt/requirements/
RUN python3 -m venv /opt/manim \
    && /opt/manim/bin/python -m pip install --no-cache-dir \
      -r /opt/requirements/requirements.lock -r /opt/requirements/requirements-test.lock
# System Poppler introspection stays separate from the locked Manim environment.
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3-gi gir1.2-poppler-0.18 \
    && rm -rf /var/lib/apt/lists/*
