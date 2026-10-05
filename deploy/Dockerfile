# AirPower application image: API, dashboard, models. Build from the repository root:
#   docker build -f deploy/Dockerfile -t airpower:0.2 .
# Needs, at run time (mounted, not baked in): the store or its database, outputs/models, and data/ (C-MAPSS replay
# trajectories); the CWRU / SEU / NASA battery sets from AeroTwin's data folder are optional (see docs/OPERATIONS.md).

# ---- dashboard
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- application
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin airpower
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && pip install setuptools wheel \
 && pip install --no-deps --ignore-requires-python progpy==1.7.1 \
 && pip install --no-deps --no-build-isolation filterpy==1.4.5
COPY pyproject.toml ./
COPY airpower/ airpower/
COPY server/ server/
COPY data_prep/ data_prep/
COPY tools/ tools/
COPY deploy/demo_users.yaml deploy/demo_users.yaml
COPY --from=web /web/dist web/dist
RUN mkdir -p outputs data && chown -R airpower:airpower /app/outputs
USER 10001
EXPOSE 8000
ENV AIRPOWER_DB=sqlite:////app/outputs/airpower.sqlite AIRPOWER_AUTH=on
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "server.app", "--host", "0.0.0.0", "--port", "8000"]
