FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ATLAS_RUNTIME_MODE=cloud \
    ATLAS_RUNTIME_ROLE=api \
    ATLAS_STEP_DELAY_SECONDS=0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY atlas ./atlas
COPY pyproject.toml .

# Default: HTTP API. Override the container command for the worker:
#   python -m uvicorn atlas.worker:app --host 0.0.0.0 --port ${PORT}
CMD ["sh", "-c", "python -m uvicorn atlas.main:app --host 0.0.0.0 --port ${PORT}"]
