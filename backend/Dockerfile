FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# FTS-Verzeichnis anlegen (persistentes Volume wird hier gemountet)
# Non-root-User (Security-Audit 040626, #5/#6) — der Host-Bind-Mount für /app/fts
# muss uid 10001 gehören (Prod: chown -R 10001:10001 /root/mailflow/mailflow-backend/fts)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/fts \
    && chown appuser:appuser /app/fts

USER appuser

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
