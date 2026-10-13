# 1) Build the embeddable widget (React -> one JS file)
FROM node:22-slim AS widget
WORKDIR /widget
COPY widget/package.json widget/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY widget/ ./
RUN npm run build

# 2) Python API, serving the widget at /widget/
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/opt/fastembed
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY --from=widget /widget/dist widget/dist
# Bake the embedding model into the image so containers start without downloading it.
RUN python -c "from fastembed import TextEmbedding; from app.config import EMBED_MODEL; TextEmbedding(EMBED_MODEL)"
RUN useradd --create-home appuser && mkdir -p index logs && chown -R appuser index logs /opt/fastembed
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
