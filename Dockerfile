# Playwright image ships Chromium and its system dependencies.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Madrid

COPY requirements.txt .
# x86_64: real Google Chrome (its certificate root store stays current).
# ARM64 (e.g. Oracle Cloud Ampere, free tier): Google ships no Linux Chrome for ARM, so the
# bundled Chromium from the base image is used (BROWSER_CHANNEL=auto picks it at runtime).
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$(uname -m)" = "x86_64" ]; then playwright install --with-deps chrome; \
       else playwright install chromium; fi

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini run_monitor.py ./

RUN useradd -m vale && chown -R vale /app
USER vale

EXPOSE 8000
# API: stateless, scale with more replicas or WEB_CONCURRENCY. Workers and monitor override CMD.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers ${WEB_CONCURRENCY:-2}"]
