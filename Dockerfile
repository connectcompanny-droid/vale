# Playwright image ships Chromium and its system dependencies.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Madrid

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker.py .

RUN useradd -m vale && chown -R vale /app
USER vale

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
