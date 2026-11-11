FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/opt/huggingface WHISPER_MODEL=tiny.en WHISPER_CHUNK_SECONDS=15
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libmediainfo0v5 libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Preload the default model once at build time; no private video is involved.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"
COPY . .
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /opt/huggingface
USER appuser
EXPOSE 8080
CMD ["python", "review_server.py"]
