FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd -r churnops && useradd -r -g churnops churnops

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R churnops:churnops /app
USER churnops

ENTRYPOINT ["python", "train.py"]
