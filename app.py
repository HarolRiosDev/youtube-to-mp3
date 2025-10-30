import os
import json
import shutil
import tempfile
import zipfile
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, error as ID3Error

app = FastAPI(title="YouTube → MP3 API")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Urls(BaseModel):
    urls: List[str]

def is_allowed_url(u: str) -> bool:
    u = u.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def embed_metadata(mp3_path: Path, info: dict, thumb_path: Optional[Path]):
    title = info.get("title")
    artist = info.get("artist") or info.get("uploader")
    album = info.get("album") or "YouTube"
    webpage_url = info.get("webpage_url")

    audio = EasyID3(mp3_path)
    if title: audio["title"] = title
    if artist: audio["artist"] = artist
    if album: audio["album"] = album
    if webpage_url: audio["website"] = webpage_url
    audio.save(v2_version=3)

    if thumb_path and thumb_path.exists():
        try:
            audio_id3 = ID3(mp3_path)
            with open(thumb_path, "rb") as img:
                audio_id3.add(APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img.read()
                ))
            audio_id3.save(v2_version=3)
        except ID3Error:
            pass

def run_yt_dlp_to_mp3(url: str, outdir: Path) -> Path:
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
        "--no-warnings",
        url
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="ignore")[:200])

    mp3_file = next((f for f in outdir.iterdir() if f.suffix == ".mp3"), None)
    info_file = next((f for f in outdir.iterdir() if f.suffix == ".json"), None)
    thumb_file = next((f for f in outdir.iterdir() if f.suffix in [".jpg", ".jpeg", ".webp"]), None)

    if not mp3_file:
        raise RuntimeError("No se generó archivo MP3")

    if info_file:
        try:
            with open(info_file, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            embed_metadata(mp3_file, info, thumb_file)
        except Exception as e:
            print("[WARN] metadatos:", e)

    return mp3_file

@app.get("/api/health")
async def health():
    return {"status": "ok"}

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

    try:
        for u in urls:
            try:
                mp3 = run_yt_dlp_to_mp3(u, job_dir)
                results.append(mp3)
            except Exception as e:
                print("[ERROR]", e)
                results.append({"url": u, "error": str(e)})

        success = [r for r in results if isinstance(r, Path)]
        if not success:
            return JSONResponse(status_code=500, content={"detail": "No se pudieron convertir los enlaces", "results": results})

        if len(success) == 1:
            mp3_path = success[0]
            return FileResponse(mp3_path, filename=mp3_path.name, media_type="audio/mpeg")

        zip_path = job_dir / "descargas.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in success:
                zf.write(p, arcname=p.name)

        return FileResponse(zip_path, filename="descargas.zip", media_type="application/zip")

    finally:
        pass
