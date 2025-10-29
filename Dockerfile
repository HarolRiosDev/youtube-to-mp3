# Dockerfile
FROM python:3.11-slim

# Instalar ffmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiamos requirements e instalamos
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos la app
COPY . .

# Puerto en el que Render expone la app (Render proporciona $PORT en runtime)
ENV PORT=10000

# Usamos Gunicorn con workers uvicorn para aceptar $PORT
CMD ["sh", "-lc", "exec gunicorn -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8"]
