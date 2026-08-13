FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MESSJAR_DB=/data/bus.db \
    HOST=0.0.0.0 \
    PORT=7420

RUN mkdir -p /data

COPY pyproject.toml README.md ./
COPY messjar ./messjar

RUN pip install --no-cache-dir .

EXPOSE 7420

# Railway injects PORT; volume should mount at /data for SQLite durability
CMD ["mj", "bus", "serve", "--db", "/data/bus.db", "--host", "0.0.0.0"]
