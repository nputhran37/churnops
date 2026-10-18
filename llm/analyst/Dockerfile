FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PROMPT_PATH=/app/llm/prompts/drift_analyst/v1.yaml

RUN groupadd -r churnops && useradd -r -g churnops churnops

WORKDIR /app

COPY llm/analyst/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY llm/analyst/ .
COPY llm/prompts/ ./llm/prompts/

RUN chown -R churnops:churnops /app
USER churnops

EXPOSE 8080

CMD ["sh", "-c", "uvicorn service:app --host 0.0.0.0 --port ${PORT:-8080}"]
