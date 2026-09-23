import asyncio
import json
import math
import shutil
import stat
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class MediaError(ValueError):
    pass


class MediaStorageError(OSError):
    pass


def telegram_video_directory(root: Path) -> Path:
    root = root.resolve(strict=True)
    directory = root / "post-uploader" / "videos"
    # Generated files must never follow a link into Bot API data or outside the volume.
    if (root / "post-uploader").is_symlink() or directory.is_symlink():
        raise MediaError("Telegram conversion directory must not be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_telegram_video_path(path: Path, root: Path) -> Path:
    directory = telegram_video_directory(root)
    if path.parent != directory or path.is_symlink():
        raise MediaError("Converted video is outside the Telegram conversion directory.")
    return safe_media_path(path, root)


def cleanup_telegram_partials(root: Path):
    # Called only at startup, while the worker holds its exclusive database lock.
    for path in telegram_video_directory(root).glob("*.partial.mp4"):
        if path.name.removesuffix(".partial.mp4").isdecimal():
            path.unlink()


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
    pixel_format: str = ""
    audio_codec: str | None = None

    def telegram_error(self) -> str | None:
        if (
            "mp4" not in self.format.split(",")
            or self.codec != "h264"
            or self.pixel_format != "yuv420p"
            or self.audio_codec not in {None, "aac"}
        ):
            return "Could not produce a Telegram-compatible MP4 video. Export and resend it."
        return None

    def telegram_parameters(self) -> dict:
        width, height = round(self.width * self.sample_aspect_ratio), self.height
        if self.rotation % 180:
            width, height = height, width
        return {
            "duration": math.ceil(self.duration),
            "width": width,
            "height": height,
            "supports_streaming": True,
        }

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
        "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_aspect_ratio,pix_fmt:"
        "stream_disposition=attached_pic:"
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
        stream = next(
            item
            for item in payload["streams"]
            if item.get("codec_type") == "video"
            and not item.get("disposition", {}).get("attached_pic")
        )
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
            stream.get("pix_fmt", ""),
            next(
                (
                    item["codec_name"]
                    for item in payload["streams"]
                    if item.get("codec_type") == "audio"
                ),
                None,
            ),
        )
    except (
        ValueError,
        KeyError,
        StopIteration,
        ZeroDivisionError,
        TypeError,
        AttributeError,
    ) as error:
        raise MediaError("Video metadata is incomplete; send a standard playable video.") from error


CONVERSION_TIMEOUT = 3600


async def prepare_telegram_video(
    source: Path, video: Video, target: Path, *, max_size: int, disk_reserve: int
) -> Video:
    if not math.isfinite(video.rotation) or video.rotation % 90:
        raise MediaError("Export this video upright before sending it to Telegram.")
    partial = target.with_suffix(".partial.mp4")
    # Only one conversion runs at a time; a leftover partial belongs to an interrupted run.
    partial.unlink(missing_ok=True)

    def check_storage(required=0):
        if shutil.disk_usage(target.parent).free < disk_reserve + required:
            raise MediaStorageError("Insufficient free storage for Telegram video conversion.")
        if partial.exists() and partial.stat().st_size > max_size:
            raise MediaError("Converted video exceeds the configured file-size limit.")

    check_storage(video.size)
    copy_video = (
        video.codec == "h264"
        and video.pixel_format == "yuv420p"
        and video.width % 2 == 0
        and video.height % 2 == 0
    )
    arguments = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
    ]
    if copy_video:
        arguments += ["-c:v", "copy"]
    else:
        arguments += [
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-preset",
            "medium",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        ]
    arguments += (
        ["-c:a", "copy"] if video.audio_codec in {None, "aac"} else ["-c:a", "aac", "-b:a", "192k"]
    )
    arguments += ["-movflags", "+faststart", "-f", "mp4", str(partial)]
    process = None
    waiter = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        waiter = asyncio.create_task(process.wait())
        async with asyncio.timeout(CONVERSION_TIMEOUT):
            while not waiter.done():
                check_storage()
                await asyncio.wait({waiter}, timeout=0.25)
        if process.returncode:
            raise MediaError("Could not convert this video for Telegram. Export and resend it.")
        check_storage()
        prepared = await inspect_video(partial, partial.stat().st_size, max_size)
        if reason := prepared.telegram_error():
            raise MediaError(reason)
        if abs(prepared.duration - video.duration) > max(1, 2 / video.frames_per_second) or (
            prepared.audio_codec is None
        ) != (video.audio_codec is None):
            raise MediaError("Telegram conversion did not preserve the complete video and audio.")
        partial.replace(target)
        return prepared
    except TimeoutError:
        raise MediaError("Telegram conversion timed out. Export as MP4 and resend it.") from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if waiter is not None:
            await waiter
        partial.unlink(missing_ok=True)
