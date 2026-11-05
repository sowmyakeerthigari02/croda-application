# Playwright's image ships Chromium and its system libraries. Keep this tag equal to the
# playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV APP_ENV=production DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
