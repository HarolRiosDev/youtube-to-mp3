import os
import json
import tempfile
import zipfile
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, error as ID3Error

import logging

# -----------------------
# Configuración básica
# -----------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-mp3")

COOKIES_PATH = Path("/etc/secrets/cookies.txt")

app = FastAPI(title="YouTube → MP3 API")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------
# Modelos
# -----------------------

class Urls(BaseModel):
    urls: List[str]

# -----------------------
# Utilidades
# -----------------------

def is_allowed_url(u: str) -> bool:
    u = u.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def embed_metadata(mp3_path: Path, info: dict, thumb_path: Optional[Path]):
    audio = EasyID3(mp3_path)

    if info.get("title"):
        audio["title"] = info["title"]
    if info.get("artist") or info.get("uploader"):
        audio["artist"] = info.get("artist") or info.get("uploader")
    audio["album"] = info.get("album") or "YouTube"

    if info.get("webpage_url"):
        audio["website"] = info["webpage_url"]

    audio.save(v2_version=3)

    if thumb_path and thumb_path.exists():
        try:
            audio_id3 = ID3(mp3_path)
            with open(thumb_path, "rb") as img:
                audio_id3.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=img.read(),
                    )
                )
            audio_id3.save(v2_version=3)
        except ID3Error as e:
            logger.warning(f"No se pudo incrustar la carátula: {e}")

# -----------------------
# yt-dlp
# -----------------------

def run_yt_dlp_to_mp3(url: str, outdir: Path) -> Path:
    out_template = str(outdir / "%(title).200s [%(id)s].%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "bestaudio[ext=m4a]/bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192",
        "--output", out_template,
        "--write-info-json",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--no-warnings",
        "--force-overwrites",
        "--no-part",
    ]

    # 👉 Añadir cookies solo si existen
    cookies_file = None

    if COOKIES_PATH.exists():
      cookies_file = outdir / "cookies.txt"
      shutil.copy(COOKIES_PATH, cookies_file)

      logger.info("Usando cookies para yt-dlp (copiadas a tmp)")
      cmd.extend(["--cookies", str(cookies_file)])
    else:
        logger.info("Ejecutando yt-dlp sin cookies")

    cmd.append(url)

    process = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if process.returncode != 0:
        stderr = process.stderr.strip()
        logger.error(f"yt-dlp error: {stderr}")

        if "Sign in to confirm you’re not a bot" in stderr:
            raise RuntimeError(
                "YouTube requiere autenticación. "
                "Las cookies son inválidas o han expirado."
            )

        raise RuntimeError(stderr[:300])

    mp3_file = next((f for f in outdir.iterdir() if f.suffix == ".mp3"), None)
    info_file = next((f for f in outdir.iterdir() if f.suffix == ".json"), None)
    thumb_file = next(
        (f for f in outdir.iterdir() if f.suffix in [".jpg", ".jpeg", ".webp"]),
        None,
    )

    if not mp3_file:
        raise RuntimeError("No se generó el archivo MP3")

    if info_file:
        try:
            with open(info_file, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            embed_metadata(mp3_file, info, thumb_file)
        except Exception as e:
            logger.warning(f"Error al incrustar metadatos: {e}")

    return mp3_file

# -----------------------
# Endpoints
# -----------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "cookies_loaded": COOKIES_PATH.exists()
    }

@app.post("/api/convert")
async def convert(payload: Urls):
    urls = payload.urls

    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
    if len(urls) > 10:
        raise HTTPException(status_code=400, detail="Máximo 10 URLs por solicitud")

    for u in urls:
        if not is_allowed_url(u):
            raise HTTPException(status_code=400, detail=f"URL no permitida: {u}")

    job_dir = Path(tempfile.mkdtemp(prefix="yt2mp3_"))
    results = []

    for u in urls:
        try:
            mp3 = run_yt_dlp_to_mp3(u, job_dir)
            results.append(mp3)
        except Exception as e:
            results.append({"url": u, "error": str(e)})

    success = [r for r in results if isinstance(r, Path)]

    if not success:
        return JSONResponse(
            status_code=500,
            content={
                "detail": "No se pudieron convertir los enlaces",
                "results": results,
            },
        )

    if len(success) == 1:
        mp3 = success[0]
        filename = quote(mp3.name)
        return FileResponse(
            mp3,
            media_type="audio/mpeg",
            headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    zip_path = job_dir / "descargas.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in success:
            zf.write(f, arcname=f.name)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="descargas.zip"'
        },
    )
