# Runtime image for both the API and the worker.
# The API runs the default command; the worker overrides it with
# `python -m org_memory.workers.run` (see docker-compose.yml).

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

# Run as a non-root user so a container escape has no root privileges.
RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app
COPY --from=builder /install /usr/local
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY contracts ./contracts

USER app
EXPOSE 8000

# Settings are validated at import time, so a missing required variable
# stops the container before it serves a single request.
CMD ["uvicorn", "org_memory.main:app", "--host", "0.0.0.0", "--port", "8000"]
