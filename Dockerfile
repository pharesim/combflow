FROM python:3.12-slim AS builder

WORKDIR /combflow

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the embedding model so containers start without network access.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r combflow && useradd -r -g combflow -d /combflow combflow

WORKDIR /combflow

# Copy installed packages and model cache from builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /root/.cache /combflow/.cache

COPY ./project    /combflow/project
COPY ./alembic    /combflow/alembic
COPY ./alembic.ini /combflow/alembic.ini
COPY ./seeds/centroids.json /combflow/seeds/centroids.json

RUN chown -R combflow:combflow /combflow

ENV HF_HOME=/combflow/.cache/huggingface
USER combflow

EXPOSE 8000
