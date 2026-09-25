# Binance Analyst - the daily CARRY-7d chain, anywhere Docker runs.
#
# The image holds code and dependencies only. Everything that changes at run time
# (ledgers, .execution/ audit and markers, caches, logs, .env.testnet) lives in the
# repo directory, which docker-compose bind-mounts at /app. Replace the container
# freely; the state stays.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Never trade as root. The bind mount must be writable by uid 1000 (see README).
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=30m --timeout=20s --start-period=1m --retries=1 \
    CMD python healthcheck.py

# Default: the always-on UTC scheduler. Override with any script, e.g.
#   docker compose run --rm bot python run_daily.py status
CMD ["python", "run_scheduler.py"]
