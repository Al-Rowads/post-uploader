import asyncio
import json
import math
import stat
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class MediaError(ValueError):
    pass


def safe_media_path(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve(strict=True))
    except ValueError:
        raise MediaError(
            "Telegram returned a file outside the configured shared media directory."
        ) from None
    # Never let cleanup touch the Bot API database, credential files, or arbitrary host files.
    if len(relative.parts) < 3 or resolved.parent.name not in {"videos", "documents"}:
        raise MediaError("Telegram returned a path outside its video/document media folders.")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise MediaError("Telegram file is not a regular file.")
    return resolved


@dataclass(frozen=True)
class Video:
    size: int
    duration: float
    width: int
    height: int
    frames_per_second: float
    codec: str
    format: str
    sample_aspect_ratio: Fraction = Fraction(1)
    rotation: float = 0

    def youtube_shorts_error(self) -> str | None:
        if not math.isfinite(self.duration) or not 0 < self.duration <= 180:
            return (
                f"YouTube Shorts require a positive duration of at most 180 seconds "
                f"(video: {self.duration} seconds). Edit and resend the video."
            )
        if self.width <= 0 or self.height <= 0 or self.sample_aspect_ratio <= 0:
            return "YouTube Shorts require valid video dimensions. Export and resend the video."
        if not math.isfinite(self.rotation) or self.rotation % 90 != 0:
            return (
                "Cannot verify YouTube Shorts orientation with this video rotation "
                f"({self.rotation:g} degrees). Export upright and resend the video."
            )
        aspect_ratio = Fraction(self.width, self.height) * self.sample_aspect_ratio
        if self.rotation % 180:
            aspect_ratio = 1 / aspect_ratio
        if aspect_ratio > 1:
            return (
                "YouTube Shorts require a square or vertical video "
                f"(video displays landscape at {aspect_ratio.numerator}:"
                f"{aspect_ratio.denominator}). Edit and resend the video."
            )
        return None

    def tiktok_error(self, max_duration: float) -> str | None:
        if not 3 <= self.duration <= min(600, max_duration):
            return f"TikTok requires a duration between 3 and {min(600, max_duration):g} seconds."
        if not (360 <= self.width <= 4096 and 360 <= self.height <= 4096):
            return "TikTok requires video dimensions between 360 and 4096 pixels."
        if not 23 <= self.frames_per_second <= 60:
            return "TikTok requires a frame rate between 23 and 60 fps."
        if self.codec not in {"h264", "hevc", "vp8"}:
            return f"TikTok does not support this video codec ({self.codec})."
        if not set(self.format.split(",")) & {"mp4", "mov", "webm"}:
            return "TikTok requires an MP4, MOV, or WebM container."
        return None


async def inspect_video(path: Path, expected_size: int, max_size: int) -> Video:
    size = path.stat().st_size
    if size == 0 or size != expected_size:
        raise MediaError(
            "Downloaded file size does not match Telegram's file size; resend the video."
        )
    if size > max_size:
        raise MediaError("Video exceeds the configured file-size limit.")
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_entries",
        "format=duration,format_name:"
        "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_aspect_ratio:"
        "stream_side_data=rotation:stream_tags=rotate",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(45):
            stdout, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise MediaError("The downloaded file is not a readable video.")
    try:
        payload = json.loads(stdout)
    except ValueError as error:
        raise MediaError("Video metadata is unreadable; send a standard playable video.") from error
    return video_from_probe(payload, size)


def video_from_probe(payload: dict, size: int) -> Video:
    try:
        stream = next(item for item in payload["streams"] if item.get("codec_type") == "video")
        duration = float(payload["format"]["duration"])
        fps = float(Fraction(stream["avg_frame_rate"]))
        if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError("Invalid duration or frame rate")
        width, height = int(stream["width"]), int(stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError("Invalid video dimensions")
        # Unspecified SAR uses the encoded dimensions; explicit SAR must remain authoritative.
        raw_aspect_ratio = stream.get("sample_aspect_ratio", "N/A")
        sample_aspect_ratio = (
            Fraction(1)
            if raw_aspect_ratio == "N/A"
            else Fraction(raw_aspect_ratio.replace(":", "/"))
        )
        if sample_aspect_ratio == 0:
            sample_aspect_ratio = Fraction(1)
        if sample_aspect_ratio < 0:
            raise ValueError("Invalid pixel aspect ratio")
        # A display matrix supersedes the legacy rotate tag when both are present.
        rotation = float(
            next(
                (
                    item["rotation"]
                    for item in stream.get("side_data_list", [])
                    if "rotation" in item
                ),
                stream.get("tags", {}).get("rotate", 0),
            )
        )
        return Video(
            size,
            duration,
            width,
            height,
            fps,
            stream["codec_name"],
            payload["format"]["format_name"],
            sample_aspect_ratio,
            rotation,
        )
    except (
        ValueError, KeyError, StopIteration, ZeroDivisionError, TypeError, AttributeError
    ) as error:
        raise MediaError("Video metadata is incomplete; send a standard playable video.") from error
