FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY router.py aibox_catalog.py config.yaml ./
COPY .env.example ./

EXPOSE 8320

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3   CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8320/health/live')"

CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8320"]
