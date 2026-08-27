from fastapi import FastAPI

app = FastAPI(
    title="Video Streamer API",
    description="A video streaming backend built with FastAPI",
    version="1.0.0"
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