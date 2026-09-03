FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 agent && mkdir /workspaces && chown agent:agent /workspaces
COPY pyproject.toml ./
RUN pip install --no-cache-dir .[dev]
COPY app ./app
COPY tests ./tests
RUN chmod 0755 /app/app/git/askpass.sh && pip install --no-cache-dir . && chown -R agent:agent /app
USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
