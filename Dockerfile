FROM python:3.12-slim

WORKDIR /app

# Create non-root user
RUN groupadd -g 999 docker && \
    useradd -r -u 1000 -g docker -m dockllama && \
    mkdir -p /app/data && \
    chown -R dockllama:docker /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=dockllama:docker dockllama/ dockllama/
COPY --chown=dockllama:docker frontend/ frontend/

USER dockllama

EXPOSE 8556

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8556/api/health')" || exit 1

CMD ["python", "-m", "dockllama"]
