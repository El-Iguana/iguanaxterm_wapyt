# IguanaXterm — builds from a plain clone of this repository, with Docker or
# Podman, on Linux, Windows or macOS (x86_64 or ARM). See INSTALL.md.
#
#   docker build -f Containerfile -t iguanaxterm .
#   podman build -f Containerfile -t iguanaxterm .
#
# (Docker only looks for a file named "Dockerfile" unless told; compose.yaml
# names this one for you.)
#
# wapyt, the widgetset, is not on PyPI, so the first stages fetch it from
# GitHub at a pinned commit and build the two wheels the app needs:
#   * wapyt 0.1.0 — installed into the server's Python, where pytincture's
#     widgetset discovery reads it (it must not be an editable install);
#   * wapyt 99.99.99 — the development wheel the *browser* installs from the
#     app's modules folder, with its asset manifest regenerated for that
#     version (pytincture hash-checks every widget asset).
#
# To build against a local wapyt checkout instead (scripts/podman-run.sh does
# this), replace the source stage with a directory:
#   podman build --build-context wapyt-src=../wa_pytincture_widgetset .
#   docker buildx build --build-context wapyt-src=../wa_pytincture_widgetset .

ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim

# ── wapyt source, from GitHub ────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS wapyt-git
ARG WAPYT_REPO=https://github.com/WAwesome-AI/wa_pytincture_widgetset.git
# The commit Monguana was verified against. Bump deliberately.
ARG WAPYT_REF=2a3bc1996978c71d0d52033d0056e8a5e3382539
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none "$WAPYT_REPO" /src \
    && git -C /src checkout --quiet "$WAPYT_REF" \
    && rm -rf /src/.git

# The source tree alone, at the root, so --build-context can replace it.
FROM scratch AS wapyt-src
COPY --from=wapyt-git /src /

# ── the two wapyt wheels ─────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS wapyt-wheels
COPY --from=wapyt-src / /wapyt
RUN set -eu; \
    pip wheel --no-cache-dir --no-deps /wapyt -w /wheels/server; \
    cp -r /wapyt /dev-src; \
    sed -i 's/^version = ".*"$/version = "99.99.99"/' /dev-src/pyproject.toml; \
    grep -q "pytincture-assets.json" /dev-src/MANIFEST.in \
        || sed -i '1i include wapyt/pytincture-assets.json' /dev-src/MANIFEST.in; \
    python /wapyt/scripts/generate_assets_manifest.py /dev-src; \
    pip wheel --no-cache-dir --no-deps /dev-src -w /wheels/browser; \
    ls /wheels/server/wapyt-*.whl /wheels/browser/wapyt-99.99.99-py3-none-any.whl

# ── the app ──────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

WORKDIR /app

# gcc + libffi cover the bcrypt/cryptography C extensions on architectures
# without prebuilt wheels (a no-op on x86_64 and arm64); git is needed by pip
# to install pytincture from its repository.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=wapyt-wheels /wheels/server/ /tmp/wapyt/
RUN pip install --no-cache-dir /tmp/wapyt/wapyt-*.whl && rm -rf /tmp/wapyt

COPY service.py pytincture_compat.py manage.py ./
COPY appcode/ appcode/
COPY --from=wapyt-wheels /wheels/browser/wapyt-99.99.99-py3-none-any.whl appcode/

# SQLite database, the Fernet key and the session secret live here, and so do
# folders saved on the server (/data/downloads) unless a host folder is mounted
# at /downloads. Keep it on a named volume: losing secret.key loses every
# stored credential.
RUN mkdir -p /data
VOLUME /data

EXPOSE 8765

# GANXTERM_BIND=0.0.0.0 inside the container is what makes a published port
# work; publish it on the host as 127.0.0.1:8765 (see compose.yaml).
ENV GANXTERM_DATA_DIR=/data \
    GANXTERM_BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PORT=8765

# Never run the app as root: it holds every user's SSH credentials.
RUN useradd --system --uid 10001 --home /app iguana && chown -R iguana /app /data
USER iguana

CMD ["python", "service.py"]
