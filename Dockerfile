FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKLLAMA_DATA=/app/data

WORKDIR /app

# Unprivileged app user. No USER line: the entrypoint starts as root to fix the
# data-folder owner and Docker-socket group, then drops to this user.
RUN useradd -r -u 1000 -m -s /usr/sbin/nologin dockllama \
    && mkdir -p /app/data /app/backups \
    && chown dockllama:dockllama /app/data /app/backups

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/dockllama /usr/local/bin/dockllama
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/dockllama

COPY dockllama/ dockllama/
COPY frontend/ frontend/

EXPOSE 8556 8557
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -m dockllama.healthcheck

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["dockllama", "serve"]
