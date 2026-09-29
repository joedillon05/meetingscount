# Same image runs locally and on Fargate.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY alembic ./alembic
COPY alembic.ini .
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

# Never run as root in a container a county's security review will look at.
# The entrypoint starts as root only long enough to hand the data volume to
# this user, then drops to it for the life of the container.
RUN useradd --uid 10001 --no-create-home appuser && chown -R appuser /srv \
    && chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
