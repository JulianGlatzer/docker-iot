# Daikin Altherma 4 – Modbus logger (read-only)
FROM python:3.12-slim

# tzdata: local time (TZ from .env) for the ts column and daily values (v_day)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY altherma4_read.py .

# run without root
RUN useradd --system --uid 10001 --no-create-home logger
USER logger

ENV PYTHONUNBUFFERED=1

# health check: status file is touched after every poll (see script)
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,time,sys; p='/tmp/altherma4.alive'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"

ENTRYPOINT ["python", "/app/altherma4_read.py"]
