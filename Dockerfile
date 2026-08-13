FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7420 \
    MESSJAR_REQUIRE_AUTH=1

COPY pyproject.toml README.md ./
COPY messjar ./messjar

RUN pip install --no-cache-dir .

EXPOSE 7420

# Requires Railway: DATABASE_URL (Postgres plugin) + MESSJAR_PASSWORD (shared secret)
CMD ["mj", "bus", "serve", "--host", "0.0.0.0"]
