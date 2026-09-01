from contextlib import asynccontextmanager
from pathlib import Path
import uuid

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from sqlalchemy import select
from fastapi.responses import StreamingResponse
import re
from .database import Base, engine, AsyncSessionLocal
from .models import Video
from .tasks import process_video
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse


UPLOAD_DIR = Path("storage/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await engine.dispose()


app = FastAPI(
    title="Video Streamer API",
    description="A video streaming backend built with FastAPI",
    version="1.0.0",
    lifespan=lifespan
)
app.mount(
    "/hls",
    StaticFiles(directory="storage/hls"),
    name="hls"
)
app.mount(
    "/thumbnails",
    StaticFiles(directory="storage/thumbnails"),
    name="thumbnails"
)
app.mount(
    "/frontend",
    StaticFiles(directory="frontend"),
    name="frontend"
)


@app.get("/")
async def home():
    return FileResponse("frontend/index.html")


@app.get("/health")
async def health():
    return {
        "status": "healthy"
    }


@app.post("/videos")
async def upload_video(file: UploadFile = File(...)):

    # 1. Validate file type
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(
            status_code=400,
            detail="Only video files are allowed"
        )

    # 2. Generate unique filename
    extension = Path(file.filename).suffix
    unique_filename = f"{uuid.uuid4()}{extension}"

    file_path = UPLOAD_DIR / unique_filename

    # 3. Save video to disk
    file_size = 0

    with open(file_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            buffer.write(chunk)
            file_size += len(chunk)

    # 4. Save metadata to PostgreSQL
    async with AsyncSessionLocal() as session:

        video = Video(
            filename=unique_filename,
            original_filename=file.filename,
            file_path=str(file_path),
            file_size=file_size,
            content_type=file.content_type,
            status="uploaded"
        )

        session.add(video)

        await session.commit()
        await session.refresh(video)
        process_video.delay(video.id)

    # 5. Return database information
    return {
        "id": video.id,
        "filename": video.filename,
        "original_filename": video.original_filename,
        "file_size": video.file_size,
        "content_type": video.content_type,
        "status": video.status
    }
@app.get("/videos")
async def get_videos():

    async with AsyncSessionLocal() as session:

        result = await session.execute(
            select(Video).order_by(Video.created_at.desc())
        )

        videos = result.scalars().all()

    return [
        {
            "id": video.id,
            "filename": video.filename,
            "original_filename": video.original_filename,
            "file_size": video.file_size,
            "content_type": video.content_type,
            "status": video.status,
            "created_at": video.created_at
        }
        for video in videos
    ]

@app.get("/videos/{video_id}")
async def get_video(video_id: int):

    async with AsyncSessionLocal() as session:

        result = await session.execute(
            select(Video).where(Video.id == video_id)
        )

        video = result.scalar_one_or_none()

    if video is None:
        raise HTTPException(
            status_code=404,
            detail="Video not found"
        )

    return {
        "id": video.id,
        "filename": video.filename,
        "original_filename": video.original_filename,
        "file_size": video.file_size,
        "content_type": video.content_type,
        "status": video.status,
        "created_at": video.created_at
    }

def parse_range_header(range_header: str, file_size: int):
    """
    Parse an HTTP Range header.

    Examples:
        bytes=0-999
        bytes=1000-
        bytes=-500
    """

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)

    if not match:
        raise ValueError("Invalid Range header")

    start_str, end_str = match.groups()

    # bytes=-500
    # Last 500 bytes
    if start_str == "":
        suffix_length = int(end_str)

        if suffix_length <= 0:
            raise ValueError("Invalid range")

        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    else:
        start = int(start_str)

        if start >= file_size:
            raise ValueError("Range starts beyond file")

        # bytes=1000-
        if end_str == "":
            end = file_size - 1

        else:
            end = int(end_str)

            if end >= file_size:
                end = file_size - 1

    if start > end:
        raise ValueError("Invalid range")

    return start, end


@app.get("/videos/{video_id}/stream")
async def stream_video(
    video_id: int,
    request: Request
):
    # 1. Find video in database
    async with AsyncSessionLocal() as session:

        result = await session.execute(
            select(Video).where(Video.id == video_id)
        )

        video = result.scalar_one_or_none()

    if video is None:
        raise HTTPException(
            status_code=404,
            detail="Video not found"
        )

    # 2. Check that the actual file exists
    file_path = Path(video.file_path)

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Video file not found"
        )

    file_size = file_path.stat().st_size

    # 3. Check Range header
    range_header = request.headers.get("range")

    # --------------------------------------------------
    # CASE 1: No Range header
    # --------------------------------------------------

    if range_header is None:

        def file_iterator():
            with open(file_path, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            file_iterator(),
            status_code=200,
            media_type=video.content_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            }
        )

    # --------------------------------------------------
    # CASE 2: Range header exists
    # --------------------------------------------------

    try:
        start, end = parse_range_header(
            range_header,
            file_size
        )

    except ValueError:
        return StreamingResponse(
            content=iter(()),
            status_code=416,
            headers={
                "Content-Range": f"bytes */{file_size}"
            }
        )

    content_length = end - start + 1

    def range_iterator():
        with open(file_path, "rb") as file:

            file.seek(start)

            remaining = content_length

            while remaining > 0:

                chunk_size = min(
                    1024 * 1024,
                    remaining
                )

                chunk = file.read(chunk_size)

                if not chunk:
                    break

                yield chunk

                remaining -= len(chunk)

    return StreamingResponse(
        range_iterator(),
        status_code=206,
        media_type=video.content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
        }
    )