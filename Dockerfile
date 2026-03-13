FROM python:3.12-slim

LABEL description="AutoPrint - Weekly color print to reduce nozzle clogging"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# CUPS printing stack + fonts for generated image
RUN apt-get update && apt-get install -y --no-install-recommends \
        cups \
        cups-client \
        cups-filters \
        cups-bsd \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
RUN pip install --no-cache-dir \
        "pillow>=10.0" \
        "schedule>=1.2" \
        "flask>=3.0"

COPY entrypoint.sh /entrypoint.sh
COPY autoprint.py  /app/autoprint.py

RUN chmod +x /entrypoint.sh && mkdir -p /data

WORKDIR /app

# 8080 = AutoPrint Dashboard  |  631 = CUPS Web-UI
EXPOSE 8080 631

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/status')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
