FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY gold_watch ./gold_watch
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home goldwatch && mkdir -p /app/data && chown goldwatch:goldwatch /app/data
USER goldwatch
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "gold_watch.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
