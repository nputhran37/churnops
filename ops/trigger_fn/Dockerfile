FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd -r churnops && useradd -r -g churnops churnops
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN chown -R churnops:churnops /app
USER churnops
ENV FUNCTION_TARGET=launch_retraining_pipeline FUNCTION_SIGNATURE_TYPE=http
EXPOSE 8080
CMD ["functions-framework", "--target=launch_retraining_pipeline", "--signature-type=http", "--host=0.0.0.0", "--port=8080"]
