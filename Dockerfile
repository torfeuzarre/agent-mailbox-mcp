FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MP_DB_PATH=/data/memoire.db MP_AGENTS_FILE=/config/agents.json MP_PORT=8000
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd --uid 10001 --create-home memoire && mkdir -p /data && chown memoire /data
USER memoire
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "app"]
