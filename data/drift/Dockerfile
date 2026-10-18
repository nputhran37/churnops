# Build from the repository root so the shared data-plane library ships with the service:
#   docker build -f data/drift/Dockerfile -t churnops-drift .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

RUN groupadd -r churnops && useradd -r -g churnops churnops

WORKDIR /app

COPY data/drift/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY data/common ./common
COPY contracts ./contracts
COPY data/drift ./drift

RUN chown -R churnops:churnops /app
USER churnops

WORKDIR /app/drift

# Cloud Run service: push subscription on data.batch.landed and model.promoted.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
