import os
import json
import shutil
import tempfile
import zipfile
import subprocess
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, error as ID3Error

# ---------------------------------------------
# Configuración principal
# ---------------------------------------------

app = FastAPI(title="YouTube → MP3 Converter (con progreso)")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------
# Modelos
# ---------------------------------------------

class Urls(BaseModel):
    urls: List[str]


# ---------------------------------------------
# Funciones auxiliares
# ---------------------------------------------

def is_allowed_url(u: str) -> bool:
    u = u.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)


def embed_metadata(mp3_path: Path, info: dict, thumb_path: Optional[Path]):
    title = info.get("title")
    artist = info.get("artist") or info.get("uploader")
    album = info.get("album") or "YouTube"
    webpage_url = info.get("webpage_url")

    audio = EasyID3(mp3_path)
    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if webpage_url:
        audio["website"] = webpage_url
    audio.save(v2_version=3)

    if thumb_path and thumb_path.exists():
        try:
            audio_id3 = ID3(mp3_path)
            with open(thumb_path, "rb") as img:
                audio_id3.add(APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=img.read()
                ))
            audio_id3.save(v2_version=3)
        except ID3Error as e:
            print(f"[WARN] No se pudo añadir carátula: {e}")


async def run_yt_dlp_stream(url: str, outdir: Path):
    """Ejecuta yt-dlp y genera mensajes de progreso."""
    out_template = str(outdir / "%(title).200s [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192",
        "--output", out_template,
        "--write-info-json",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--newline",  # <-- importante para ver progreso
        "--no-warnings",
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    async for line in proc.stdout:
        text = line.decode("utf-8", errors="ignore").strip()
        if text:
            yield f"data: {json.dumps({'url': url, 'progress': text})}\n\n"

    await proc.wait()

    if proc.returncode != 0:
        err = (await proc.stderr.read()).decode("utf-8", errors="ignore")
        yield f"data: {json.dumps({'url': url, 'error': err[:300]})}\n\n"
        return

    # Detectar archivos generados
    mp3_file, info_file, thumb_file = None, None, None
    for f in outdir.iterdir():
        if f.suffix.lower() == ".mp3":
            mp3_file = f
        elif f.suffix.lower() == ".json":
            info_file = f
        elif f.suffix.lower() in [".jpg", ".jpeg", ".webp"]:
            thumb_file = f

    if mp3_file and info_file:
        with open(info_file, "r", encoding="utf-8") as fh:
            info = json.load(fh)
        embed_metadata(mp3_file, info, thumb_file)
        yield f"data: {json.dumps({'url': url, 'done': mp3_file.name})}\n\n"
    else:
        yield f"data: {json.dumps({'url': url, 'error': 'No se generó archivo MP3'})}\n\n"


# ---------------------------------------------
# Endpoints
# ---------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/convert")
def convert(payload: Urls):
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
            print(f"[INFO] Procesando {u}")
            for _ in asyncio.run(run_yt_dlp_stream(u, job_dir)):
                pass  # ejecución sin streaming aquí
            mp3_path = next(job_dir.glob("*.mp3"))
            results.append(mp3_path)
        except Exception as e:
            print(f"[ERROR] {u}: {e}")
            results.append({"error": str(e), "url": u})

    if all(isinstance(r, dict) for r in results):
        return JSONResponse(status_code=500, content={
            "detail": "No se pudieron convertir los enlaces",
            "results": results
        })

    successful = [r for r in results if isinstance(r, Path)]
    if len(successful) == 1:
        mp3_path = successful[0]
        return FileResponse(mp3_path, filename=mp3_path.name, media_type="audio/mpeg")

    zip_path = job_dir / "resultados.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in successful:
            zf.write(p, arcname=p.name)

    return FileResponse(zip_path, filename="descargas.zip", media_type="application/zip")


@app.post("/api/convert/stream")
async def convert_stream(payload: Urls):
    urls = payload.urls
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    async def event_generator():
        for u in urls:
            if not is_allowed_url(u):
                yield f"data: {json.dumps({'url': u, 'error': 'URL no permitida'})}\n\n"
                continue
            tmpdir = Path(tempfile.mkdtemp(prefix="yt2mp3_"))
            async for msg in run_yt_dlp_stream(u, tmpdir):
                yield msg
            yield f"data: {json.dumps({'url': u, 'status': 'finalizado'})}\n\n"

        yield "event: end\ndata: done\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
