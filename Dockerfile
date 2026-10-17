FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 ledger
COPY app ./app
COPY public ./public
COPY run.py ./run.py
RUN mkdir -p /app/data && chown ledger:ledger /app/data
LABEL org.opencontainers.image.source="https://github.com/MeowAPI/team-ledger"
USER ledger
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/app/data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)"
CMD ["python", "run.py"]
