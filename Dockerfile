# Playwright image ships Chromium and its system dependencies.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Madrid

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # Real Google Chrome: its certificate root store stays current, unlike the frozen bundled Chromium.
    && playwright install --with-deps chrome

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini run_monitor.py ./

RUN useradd -m vale && chown -R vale /app
USER vale

EXPOSE 8000
# API: stateless, scale with more replicas or WEB_CONCURRENCY. Workers and monitor override CMD.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers ${WEB_CONCURRENCY:-2}"]
