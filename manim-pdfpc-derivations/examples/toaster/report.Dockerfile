# Build after the toolkit's ci/assets.Dockerfile; the context is this directory.
ARG AUTHORING_IMAGE=derivation-assets:local
FROM ${AUTHORING_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    PYTHON=/opt/report/bin/python3 \
    PATH=/opt/manim/bin:${PATH}

# Match the report's Pandoc, TeX engines and fonts on Ubuntu 24.04.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
      pandoc=3.1.3+ds-2 texlive-luatex=2023.20240207-1 \
      tex-gyre=20180621-6 fonts-texgyre=20180621-6 \
      fonts-texgyre-math=20180621-6 fonts-dejavu-core=2.37-8 \
      git make \
    && rm -rf /var/lib/apt/lists/*

# The report's chart/check packages have their own environment.
COPY report-requirements.lock /opt/requirements/report-requirements.lock
RUN /usr/bin/python3 -m venv /opt/report \
    && /opt/report/bin/python -m pip install --no-cache-dir --only-binary=:all: \
      -r /opt/requirements/report-requirements.lock \
    && /opt/report/bin/python -m pip check \
    && /opt/manim/bin/python -m pip check

RUN for resource in tgpagella.sty tgheros.sty mathpazo.sty fontspec.sty unicode-math.sty; do \
      kpsewhich "$resource" || exit 1; \
    done \
    && mkdir -p /opt/report-environment \
    && dpkg-query -W -f='${Package}=${Version}\n' > /opt/report-environment/system-packages.txt \
    && /opt/report/bin/python -m pip freeze --all > /opt/report-environment/python-packages.txt \
    && pandoc --version > /opt/report-environment/pandoc.txt \
    && lualatex --version > /opt/report-environment/lualatex.txt
