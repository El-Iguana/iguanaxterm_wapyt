FROM docker.io/python:3.13-slim

WORKDIR /app

# gcc + libffi cover the bcrypt/cryptography C extensions on architectures
# without prebuilt wheels. A no-op cost on x86_64 and aarch64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# wapyt is installed non-editable so pytincture's widgetset discovery can read
# __widgetset__ from the distribution metadata; an editable install exposes only
# a .pth and discovery silently yields no widgets.
COPY vendor-wheels/ /tmp/vendor-wheels/
RUN pip install --no-cache-dir /tmp/vendor-wheels/wapyt-*.whl && rm -rf /tmp/vendor-wheels

COPY service.py .
COPY appcode/ appcode/

# SQLite database, the Fernet key and the session secret live here. Mount a
# named volume so they survive an upgrade: losing secret.key loses every stored
# credential.
RUN mkdir -p /data
VOLUME /data

EXPOSE 8765

ENV GANXTERM_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8765

# Never run the app as root: it holds every user's SSH credentials.
RUN useradd --system --uid 10001 --home /app iguana && chown -R iguana /app /data
USER iguana

CMD ["python", "service.py"]
