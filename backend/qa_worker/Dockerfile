FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-calc python3 fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY worker.py /usr/local/bin/invoice-qa-worker.py
ENTRYPOINT ["python3", "/usr/local/bin/invoice-qa-worker.py"]
