FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 chatbot

WORKDIR /app

COPY pyproject.toml requirements.lock ./
# Versions figées (celles de l'image qui marchait) : un build ne doit jamais
# tirer une nouvelle version qui casse le bot (ex. sqlmodel >= 0.0.45).
RUN pip install --upgrade pip \
    && pip install -r requirements.lock

COPY app/ ./app/

RUN mkdir -p /app/data && chown -R chatbot:chatbot /app

USER chatbot

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
