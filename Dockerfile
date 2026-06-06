# ============================================================
# VoxCPM2 Voice Studio v1.4 - Docker Image
# ============================================================
FROM python:3.11-slim

LABEL org.opencontainers.image.title="VoxCPM2 Voice Studio"
LABEL org.opencontainers.image.description="Voice synthesis WebUI with voice cloning, voice design, and voice management. 中文：VoxCPM2 在线语音合成 WebUI，支持常规配音、声音设计、音色管理和 OpenAI 兼容 TTS API。"
LABEL org.opencontainers.image.version="1.4"

WORKDIR /app

# Install system deps (ffmpeg optional, for audio format conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY server.py webui.py voxcpm_client.py client.py config.yaml ./
COPY templates/ templates/
COPY voices/ voices/

# Create dirs if not exist
RUN mkdir -p voices output

EXPOSE 5000

# Default: run Web UI
CMD ["python", "webui.py"]
