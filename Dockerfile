FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml azure-aws-migration-capstone.md ./
COPY src ./src
COPY kb ./kb
COPY scripts ./scripts
COPY config ./config

RUN python -m pip install --upgrade pip \
    && pip install .

RUN groupadd --system app \
    && useradd --system --gid app --create-home app \
    && mkdir -p /app/checkpoints \
    && chown -R app:app /app

USER app

EXPOSE 8000

ENV APPROVAL_CHECKPOINT_DIR=/app/checkpoints

CMD ["uvicorn", "src.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
