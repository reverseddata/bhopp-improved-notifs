FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/reverseddata/bhopp-improved-notifs" \
      org.opencontainers.image.description="Discord sale alerts and order-chat automations for BHOPP sellers"
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -r -u 10001 notifs && mkdir -p data && chown notifs data
USER notifs
CMD ["bhopp-notifs", "run"]
