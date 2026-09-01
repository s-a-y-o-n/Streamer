from pathlib import Path
import subprocess
import json

from .celery_app import celery_app
from .celery_database import SessionLocal
from .models import Video


def update_video_status(video_id: int, status: str):

    session = SessionLocal()

    try:
        video = session.get(Video, video_id)

        if video is None:
            print(f"Video {video_id} not found")
            return

        video.status = status
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_video_resolution(input_path: Path):

    command = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(input_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFprobe failed:\n{result.stderr}"
        )

    data = json.loads(result.stdout)

    for stream in data["streams"]:
        if stream.get("codec_type") == "video":
            return stream["width"], stream["height"]

    raise RuntimeError("No video stream found")

def generate_hls(
    input_path: Path,
    output_dir: Path
):
    """
    Convert an MP4 file into an HLS playlist
    and media segments.
    """

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    playlist_path = output_dir / "playlist.m3u8"

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_path),

        "-c:v",
        "copy",

        "-c:a",
        "copy",

        "-hls_time",
        "6",

        "-hls_playlist_type",
        "vod",

        "-hls_segment_filename",
        str(output_dir / "segment_%03d.ts"),

        str(playlist_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:

        print(
            f"HLS generation failed for "
            f"{input_path}"
        )

        print(result.stderr)

        raise RuntimeError(
            "HLS generation failed"
        )

    return playlist_path

def create_master_playlist(
    hls_dir: Path,
    resolutions: list
):
    master_path = hls_dir / "master.m3u8"

    bandwidths = {
        "360p": 800000,
        "720p": 2500000,
        "1080p": 5000000,
    }

    resolution_values = {
        "360p": "640x360",
        "720p": "1280x720",
        "1080p": "1920x1080",
    }

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
    ]

    for name, _, _ in resolutions:
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidths[name]},'
            f'RESOLUTION={resolution_values[name]}'
        )
        lines.append(f"{name}/playlist.m3u8")

    master_path.write_text("\n".join(lines) + "\n")

    print(f"Master playlist created: {master_path}")

    return master_path

def generate_thumbnail(
    input_path: Path,
    output_path: Path
):
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    command = [
        "ffmpeg",
        "-y",
        "-ss", "5",
        "-i", str(input_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:

        print("Thumbnail generation failed:")
        print(result.stderr)

        raise RuntimeError(
            "Thumbnail generation failed"
        )

    print(
        f"Thumbnail generated: {output_path}"
    )

    return output_path

@celery_app.task
def process_video(video_id: int):

    print(f"Starting processing for video {video_id}")

    update_video_status(video_id, "processing")

    # ---------------------------------------
    # Get video information from PostgreSQL
    # ---------------------------------------

    session = SessionLocal()

    try:

        video = session.get(Video, video_id)

        if video is None:
            print(f"Video {video_id} not found")
            return

        input_path = Path(video.file_path)

    finally:
        session.close()

    if not input_path.exists():
        update_video_status(video_id, "failed")
        raise FileNotFoundError(
            f"Video file not found: {input_path}"
        )

    # ---------------------------------------
    # Get original resolution
    # ---------------------------------------

    width, height = get_video_resolution(input_path)

    print(
        f"Original resolution: "
        f"{width}x{height}"
    )

    thumbnail_dir = Path("storage") / "thumbnails"

    thumbnail_path = (
        thumbnail_dir /
        f"video_{video_id}.jpg"
    )

    print("Generating thumbnail...")

    generate_thumbnail(
        input_path,
        thumbnail_path
    )

    print("Thumbnail generated successfully")

    # ---------------------------------------
    # Create output directory
    # ---------------------------------------

    output_dir = (
        Path("storage") /
        "processed" /
        f"video_{video_id}"
    )


    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # ---------------------------------------
    # Decide which resolutions to generate
    # ---------------------------------------

    resolutions = []

    if height >= 360:
        resolutions.append(("360p", 640, 360))

    if height >= 720:
        resolutions.append(("720p", 1280, 720))

    if height >= 1080:
        resolutions.append(("1080p", 1920, 1080))

    if not resolutions:
        raise RuntimeError(
            "Video resolution is too low"
        )

    # ---------------------------------------
    # Generate each resolution
    # ---------------------------------------

    for name, target_width, target_height in resolutions:

        output_path = (
            output_dir /
            f"{name}.mp4"
        )

        print(
            f"Generating {name}: "
            f"{target_width}x{target_height}"
        )

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),

            "-vf",
            f"scale=w={target_width}:"
            f"h={target_height}:"
            f"force_original_aspect_ratio=decrease",

            "-c:v",
            "libx264",

            "-preset",
            "medium",

            "-crf",
            "23",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            str(output_path)
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:

            print(
                f"FFmpeg failed for {name}:"
            )

            print(result.stderr)

            update_video_status(
                video_id,
                "failed"
            )

            raise RuntimeError(
                f"FFmpeg failed for {name}"
            )

        print(
            f"{name} generated successfully"
        )

    

    # ---------------------------------------
    # Processing completed
    # ---------------------------------------
        # ---------------------------------------
    # Generate HLS
    # ---------------------------------------

    hls_dir = (
        Path("storage") /
        "hls" /
        f"video_{video_id}"
    )

    print("Generating HLS...")

    for name, _, _ in resolutions:

        input_path = (
            output_dir /
            f"{name}.mp4"
        )

        variant_dir = (
            hls_dir /
            name
        )

        generate_hls(
            input_path,
            variant_dir
        )

        print(
            f"HLS generated for {name}"
        )
    create_master_playlist(hls_dir, resolutions)
    print("Master HLS playlist generated")

    update_video_status(
        video_id,
        "ready"
    )

    print(
        f"Video {video_id} processing complete"
    )

    return {
        "video_id": video_id,
        "status": "ready",
        "resolutions": [
            resolution[0]
            for resolution in resolutions
        ]
    }