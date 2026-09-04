FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies are installed before the source is copied so that editing
# application code does not invalidate the (slow) pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user: a container process that does not need root
# should not have it.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/healthz', timeout=4).status==200 else 1)"

# Two workers for concurrency. The APScheduler jobs deliberately do NOT run
# here - see the `worker` service - because each gunicorn worker is a separate
# process and would otherwise run every job N times over.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", \
     "--access-logfile", "-", "--error-logfile", "-", "backend.app:app"]
