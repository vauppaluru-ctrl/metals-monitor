FROM python:3.11-slim

WORKDIR /app/metals_monitor

# Install dependencies first (layer cache — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY metals_live_monitor.py .
COPY metals_web_server.py .

# Runtime directories (state + logs)
RUN mkdir -p metals_monitor_state metals_monitor_logs metals_backtest_output

ENV PYTHONUNBUFFERED=1 \
    SCHEDULER_ENABLED=true \
    SCHEDULER_INTERVAL_SECS=3600 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status', timeout=8)"

CMD ["python", "metals_web_server.py"]
