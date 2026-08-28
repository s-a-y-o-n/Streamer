import os
import uuid

from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from contextlib import asynccontextmanager

from .database import Base, engine
from . import models

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


@app.get("/")
async def root():
    return {
        "message": "Video Streamer API is running"
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy"
    }

@app.post("/videos")
async def upload_video(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(
            status_code=400,
            detail="Only video files are allowed"
        )

    extension = Path(file.filename).suffix
    unique_filename = f"{uuid.uuid4()}{extension}"

    file_path = UPLOAD_DIR / unique_filename

    with open(file_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            buffer.write(chunk)

    return {
        "filename": unique_filename,
        "original_filename": file.filename,
        "content_type": file.content_type
    }