# ---- Stage 1: build dependencies into an isolated venv ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc is needed only at build time (some pyparsing/cython wheels require it on slim).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .


# ---- Stage 2: lean runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user
RUN useradd --create-home --uid 1000 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Static frontend (legacy index.html) and default catalog ship in the image.
# Both can be overridden by bind-mounting from the host (see docker-compose.yml).
COPY --chown=app:app web ./web
COPY --chown=app:app prices_catalog.json ./prices_catalog.json
COPY --chown=app:app gunicorn.conf.py ./gunicorn.conf.py

# orders.csv is a runtime-mutable artifact. Pre-create it so a non-root container
# can write to it even when no host volume is mounted.
RUN touch /app/orders.csv && chown app:app /app/orders.csv

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0) if urllib.request.urlopen('http://localhost:8080/api/health', timeout=3).status == 200 else sys.exit(1)" \
    || exit 1

CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "laser_calc_api.wsgi:app"]
