# ===== Base Image =====
FROM python:3.11-slim

# ===== Sistema y dependencias =====
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    build-essential \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== Entorno y seguridad =====
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PORT=10000

WORKDIR /app

# ===== Instalación de dependencias =====
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir yt-dlp mutagen

# ===== Copia del código =====
COPY . .

# ===== yt-dlp: actualización ligera (importante en Render) =====
RUN yt-dlp -U || true

# ===== Exposición de puerto =====
EXPOSE 10000

# ===== Comando de arranque =====
CMD ["sh", "-lc", "exec gunicorn app:app \
  -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:$PORT \
  --workers 1 \
  --threads 2 \
  --timeout 300 \
  --graceful-timeout 300"]
