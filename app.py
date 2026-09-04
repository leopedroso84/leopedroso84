from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

APP_NAME = "Baixar Mídia Shortcut"
VERSION = "1.0.0"
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/baixar-midia"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE = int(os.getenv("MAX_FILE_AGE_SECONDS", "3600"))
MAX_SECONDS = int(os.getenv("EXTRACT_TIMEOUT_SECONDS", "180"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_10_MIN", "20"))

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "threads.net", "threads.com", "tiktok.com",
    "pinterest.com", "pin.it", "reddit.com", "redd.it", "vimeo.com",
    "twitch.tv", "snapchat.com", "tumblr.com",
)

MEDIA_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
_rate: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(title=APP_NAME, version=VERSION, docs_url="/docs")


class ResolveRequest(BaseModel):
    url: HttpUrl


def allowed_url(raw: str) -> bool:
    host = (urlparse(raw).hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def cleanup() -> None:
    now = time.time()
    for p in DATA_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > MAX_AGE:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        except OSError:
            pass


def rate_limit(ip: str) -> None:
    now = time.time()
    q = _rate[ip]
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Muitas solicitações. Aguarde alguns minutos e tente novamente.")
    q.append(now)


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=MAX_SECONDS, check=False
    )
    return proc.returncode, proc.stdout[-12000:]


def files_in(folder: Path) -> list[Path]:
    result = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0:
            result.append(p)
    return sorted(result, key=lambda p: p.stat().st_mtime)


def extract_ytdlp(url: str, folder: Path) -> tuple[list[Path], str]:
    template = str(folder / "%(title).120s [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--restrict-filenames",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", template, url,
    ]
    code, log = run(cmd, folder)
    return files_in(folder), log


def extract_gallery(url: str, folder: Path) -> tuple[list[Path], str]:
    cmd = [
        "gallery-dl",
        "--dest", str(folder),
        "--filename", "{filename}.{extension}",
        url,
    ]
    code, log = run(cmd, folder)
    return files_in(folder), log


async def extract_threads_embed(url: str, folder: Path) -> list[Path]:
    # Fallback para post público do Threads quando houver imagem direta no embed.
    clean = url.split("?")[0].rstrip("/")
    embed = clean + "/embed"
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+https://www.facebook.com/externalhit_uatext.php)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
        r = await client.get(embed)
        if r.status_code != 200:
            return []
        html = r.text
        candidates = []
        for pattern in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)',
        ):
            candidates.extend(re.findall(pattern, html, flags=re.I))
        out = []
        seen = set()
        for idx, media_url in enumerate(candidates[:10]):
            media_url = media_url.replace("&amp;", "&")
            if media_url in seen:
                continue
            seen.add(media_url)
            try:
                rr = await client.get(media_url)
                if rr.status_code != 200 or not rr.content:
                    continue
                ctype = rr.headers.get("content-type", "").split(";")[0]
                ext = mimetypes.guess_extension(ctype) or Path(urlparse(media_url).path).suffix or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                path = folder / f"threads-{idx+1}{ext}"
                path.write_bytes(rr.content)
                if path.suffix.lower() in MEDIA_EXTS:
                    out.append(path)
            except Exception:
                continue
        return out


async def extract(url: str, folder: Path) -> tuple[list[Path], str]:
    logs = []
    host = (urlparse(url).hostname or "").lower()

    # 1) yt-dlp: melhor para vídeos e áudio/vídeo combinado.
    try:
        found, log = await asyncio.to_thread(extract_ytdlp, url, folder)
        logs.append("yt-dlp:\n" + log)
        if found:
            return found, "\n".join(logs)
    except Exception as exc:
        logs.append(f"yt-dlp exception: {exc}")

    # 2) gallery-dl: melhor para fotos, carrosséis e galerias.
    try:
        found, log = await asyncio.to_thread(extract_gallery, url, folder)
        logs.append("gallery-dl:\n" + log)
        if found:
            return found, "\n".join(logs)
    except Exception as exc:
        logs.append(f"gallery-dl exception: {exc}")

    # 3) Threads: fallback no embed público para imagens/vídeos diretos.
    if "threads." in host:
        try:
            found = await extract_threads_embed(url, folder)
            if found:
                return found, "\n".join(logs)
        except Exception as exc:
            logs.append(f"threads embed exception: {exc}")

    return [], "\n".join(logs)


@app.get("/")
def root():
    return {"name": APP_NAME, "version": VERSION, "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.post("/api/v1/resolve")
async def resolve(payload: ResolveRequest, request: Request):
    cleanup()
    ip = request.client.host if request.client else "unknown"
    rate_limit(ip)

    raw_url = str(payload.url)
    if not allowed_url(raw_url):
        raise HTTPException(400, "Este domínio não está habilitado no Baixar Mídia Shortcut.")

    job = uuid.uuid4().hex
    folder = DATA_DIR / job
    folder.mkdir(parents=True, exist_ok=True)

    found, logs = await extract(raw_url, folder)
    if not found:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(
            422,
            "Não consegui extrair a mídia. O conteúdo pode ser privado, exigir login ou a plataforma pode ter mudado."
        )

    base = str(request.base_url).rstrip("/")
    items = []
    for p in found[:30]:
        items.append({
            "name": p.name,
            "url": f"{base}/files/{job}/{p.name}",
            "type": "media",
        })

    return {
        "status": "ok",
        "version": VERSION,
        "count": len(items),
        "items": items,
    }


@app.get("/files/{job}/{filename}")
def get_file(job: str, filename: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job):
        raise HTTPException(404)
    safe = Path(filename).name
    path = DATA_DIR / job / safe
    if not path.exists() or not path.is_file():
        # gallery-dl pode criar subpastas; procura somente pelo basename dentro do job.
        matches = [p for p in (DATA_DIR / job).rglob(safe) if p.is_file()]
        if not matches:
            raise HTTPException(404)
        path = matches[0]
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, filename=path.name, media_type=media_type or "application/octet-stream")
