# Build from the repository root so the shared data-plane library ships with the service:
#   docker build -f data/generator/Dockerfile -t churnops-generator .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

RUN groupadd -r churnops && useradd -r -g churnops churnops

WORKDIR /app

COPY data/generator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY data/common ./common
COPY contracts ./contracts
COPY data/generator ./generator

RUN chown -R churnops:churnops /app
USER churnops

WORKDIR /app/generator

ENTRYPOINT ["python", "generator.py"]
CMD ["--scenario", "none"]
