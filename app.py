from __future__ import annotations

import asyncio
import html
import mimetypes
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
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
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT_SECONDS", "180"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_10_MIN", "20"))

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "threads.net", "threads.com", "tiktok.com",
    "pinterest.com", "pin.it", "reddit.com", "redd.it", "vimeo.com",
    "twitch.tv", "snapchat.com", "tumblr.com",
)

MEDIA_EXTS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
}

VIDEO_FIRST_HOSTS = (
    "youtube.com", "youtu.be", "tiktok.com", "vimeo.com",
    "twitch.tv", "reddit.com", "redd.it",
)

JOBS: dict[str, dict] = {}
RATE: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(title=APP_NAME, version=VERSION, docs_url="/docs")


class ResolveRequest(BaseModel):
    url: HttpUrl


def host_of(raw: str) -> str:
    return (urlparse(raw).hostname or "").lower().removeprefix("www.")


def allowed_url(raw: str) -> bool:
    host = host_of(raw)
    return any(host == item or host.endswith("." + item) for item in ALLOWED_HOSTS)


def cleanup() -> None:
    now = time.time()

    for job_id, item in list(JOBS.items()):
        if now - item.get("created_at", now) > MAX_AGE:
            JOBS.pop(job_id, None)

    for path in DATA_DIR.iterdir():
        try:
            if now - path.stat().st_mtime > MAX_AGE:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        except OSError:
            pass


def enforce_rate_limit(ip: str) -> None:
    now = time.time()
    bucket = RATE[ip]

    while bucket and now - bucket[0] > 600:
        bucket.popleft()

    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(
            429,
            "Muitas solicitações em pouco tempo. Aguarde alguns minutos e tente novamente.",
        )

    bucket.append(now)


def run_process(command: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=EXTRACT_TIMEOUT,
        check=False,
    )
    return proc.returncode, proc.stdout[-15000:]


def collect_media(folder: Path) -> list[Path]:
    result: list[Path] = []

    for path in folder.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in MEDIA_EXTS
            and path.stat().st_size > 0
        ):
            result.append(path)

    return sorted(result, key=lambda p: p.stat().st_mtime)


def run_ytdlp(url: str, folder: Path) -> tuple[list[Path], str]:
    template = str(folder / "%(title).120s [%(id)s].%(ext)s")

    command = [
        "yt-dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", template,
        url,
    ]

    _, log = run_process(command, folder)
    return collect_media(folder), log


def run_gallery_dl(url: str, folder: Path) -> tuple[list[Path], str]:
    command = [
        "gallery-dl",
        "--dest", str(folder),
        "--filename", "{filename}.{extension}",
        url,
    ]

    _, log = run_process(command, folder)
    return collect_media(folder), log


async def threads_embed_fallback(url: str, folder: Path) -> list[Path]:
    clean_url = url.split("?")[0].rstrip("/")
    embed_url = clean_url + "/embed"

    headers = {
        "User-Agent": (
            "facebookexternalhit/1.1 "
            "(+https://www.facebook.com/externalhit_uatext.php)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25,
        headers=headers,
    ) as client:
        response = await client.get(embed_url)
        if response.status_code != 200:
            return []

        page = response.text
        candidates: list[str] = []

        patterns = (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::url)?["\']',
        )

        for pattern in patterns:
            candidates.extend(re.findall(pattern, page, flags=re.I))

        output: list[Path] = []
        seen: set[str] = set()

        for index, media_url in enumerate(candidates[:12], start=1):
            media_url = html.unescape(media_url)

            if media_url in seen:
                continue
            seen.add(media_url)

            try:
                media_response = await client.get(media_url)
                if media_response.status_code != 200 or not media_response.content:
                    continue

                content_type = (
                    media_response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                )

                extension = (
                    mimetypes.guess_extension(content_type)
                    or Path(urlparse(media_url).path).suffix
                    or ".jpg"
                )

                if extension == ".jpe":
                    extension = ".jpg"

                destination = folder / f"threads-{index}{extension}"
                destination.write_bytes(media_response.content)

                if destination.suffix.lower() in MEDIA_EXTS:
                    output.append(destination)
            except Exception:
                continue

        return output


async def extract(url: str, folder: Path) -> tuple[list[Path], str]:
    logs: list[str] = []
    host = host_of(url)

    if any(host == h or host.endswith("." + h) for h in VIDEO_FIRST_HOSTS):
        order = ("ytdlp", "gallery")
    else:
        order = ("gallery", "ytdlp")

    for engine in order:
        try:
            if engine == "ytdlp":
                files, log = await asyncio.to_thread(run_ytdlp, url, folder)
                logs.append("yt-dlp:\n" + log)
            else:
                files, log = await asyncio.to_thread(run_gallery_dl, url, folder)
                logs.append("gallery-dl:\n" + log)

            if files:
                return files, "\n".join(logs)
        except Exception as exc:
            logs.append(f"{engine} exception: {exc}")

    if "threads." in host:
        try:
            files = await threads_embed_fallback(url, folder)
            if files:
                return files, "\n".join(logs)
        except Exception as exc:
            logs.append(f"threads embed exception: {exc}")

    return [], "\n".join(logs)


async def process_job(job_id: str, url: str) -> None:
    folder = DATA_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)

    JOBS[job_id]["status"] = "processing"

    try:
        files, logs = await extract(url, folder)

        if not files:
            print(f"[extract-error] job={job_id} url={url}\n{logs[-8000:]}", flush=True)
            shutil.rmtree(folder, ignore_errors=True)
            JOBS[job_id].update(
                status="error",
                message=(
                    "Não consegui extrair a mídia. O conteúdo pode ser privado, "
                    "exigir login ou a plataforma pode ter alterado a página."
                ),
                debug=logs[-4000:],
            )
            return

        JOBS[job_id].update(
            status="done",
            files=[str(path.relative_to(folder)) for path in files[:30]],
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(folder, ignore_errors=True)
        JOBS[job_id].update(
            status="error",
            message="O download ultrapassou o tempo limite. Tente novamente.",
        )
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        JOBS[job_id].update(
            status="error",
            message="Ocorreu um erro ao processar esta mídia.",
            debug=str(exc)[:2000],
        )


@app.get("/")
def root():
    return {
        "name": APP_NAME,
        "version": VERSION,
        "status": "ok",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": VERSION,
    }


@app.post("/api/v1/jobs")
async def create_job(payload: ResolveRequest, request: Request):
    cleanup()

    ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(ip)

    raw_url = str(payload.url)

    if not allowed_url(raw_url):
        raise HTTPException(
            400,
            "Este domínio não está habilitado no Baixar Mídia Shortcut.",
        )

    job_id = uuid.uuid4().hex

    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "url": raw_url,
        "created_at": time.time(),
    }

    asyncio.create_task(process_job(job_id, raw_url))

    return {
        "status": "queued",
        "jobId": job_id,
        "version": VERSION,
    }


@app.get("/api/v1/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    cleanup()

    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404)

    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Solicitação não encontrada ou expirada.")

    response = {
        "status": job["status"],
        "jobId": job_id,
        "version": VERSION,
    }

    if job["status"] == "error":
        response["message"] = job.get(
            "message",
            "Não foi possível baixar esta mídia.",
        )

    if job["status"] == "done":
        base = str(request.base_url).rstrip("/")
        response["items"] = [
            {
                "name": Path(relative).name,
                "url": f"{base}/files/{job_id}/{relative}",
                "type": "media",
            }
            for relative in job.get("files", [])
        ]
        response["count"] = len(response["items"])

    return response


@app.get("/files/{job_id}/{file_path:path}")
def download_file(job_id: str, file_path: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404)

    root = (DATA_DIR / job_id).resolve()
    path = (root / file_path).resolve()

    if root not in path.parents or not path.is_file():
        raise HTTPException(404)

    media_type, _ = mimetypes.guess_type(path.name)

    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type or "application/octet-stream",
    )
