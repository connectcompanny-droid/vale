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
COPY scripts/start.sh ./start.sh

RUN useradd -m vale && chown -R vale /app
USER vale

EXPOSE 8000
# Role chosen by VALE_ROLE (api by default); docker-compose overrides the command per service.
CMD ["sh", "./start.sh"]
