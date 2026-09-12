FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080 WHISPER_MODEL=small.en MAX_VIDEO_SECONDS=7200
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
