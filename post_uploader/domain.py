import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

PLATFORMS = ("youtube", "tiktok", "telegram")
PLATFORM_LABELS = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "telegram": "Telegram"}
CAPTION_LIMIT = 800
TERMINAL_DESTINATIONS = {"published", "failed", "invalid", "needs_action", "cancelled"}


def text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def validate_title(text: str) -> str:
    text = text.strip()
    if not text or "\n" in text or text_length(text) > 100 or "<" in text or ">" in text:
        raise ValueError("Send a single-line title of 1–100 characters, without < or >.")
    return text


def validate_platform_caption(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or text_length(text) > CAPTION_LIMIT:
        raise ValueError(f"Send a nonempty caption of at most {CAPTION_LIMIT} characters.")
    return text


def review_caption(job, destination) -> str:
    return (
        f"{PLATFORM_LABELS[destination['platform']]} · Job #{job['id']}\n"
        f"Title: {job['title']}\n\n{destination['caption']}"
    )


def parse_caption(caption: str) -> tuple[str, str]:
    caption = caption.strip()
    if not caption:
        raise ValueError(
            "Reply with a caption: first line is the title, then description/hashtags."
        )
    title = caption.splitlines()[0].strip()
    if len(title) > 100:
        raise ValueError("The first caption line (YouTube title) must be at most 100 characters.")
    if "<" in title or ">" in title:
        raise ValueError("The YouTube title cannot contain < or >.")
    if len(caption) > 2200:
        raise ValueError("The caption must be at most 2200 characters for TikTok.")
    return title, caption


def public_url(value: object, platform: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    domains = {"youtube": ("youtube.com", "youtu.be"), "tiktok": ("tiktok.com",)}[platform]
    host = parsed.hostname or ""
    if (
        parsed.scheme == "https"
        and not parsed.username
        and any(host == domain or host.endswith("." + domain) for domain in domains)
    ):
        if platform == "youtube":
            if host == "youtu.be":
                video_id = parsed.path.strip("/")
            elif parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif re.fullmatch(r"/(shorts|live|embed)/[A-Za-z0-9_-]{11}/?", parsed.path):
                video_id = parsed.path.rstrip("/").split("/")[-1]
            else:
                return None
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                return value
        elif re.fullmatch(r"/@[^/]+/video/\d+/?", parsed.path) or (
            host in {"vm.tiktok.com", "vt.tiktok.com"} and parsed.path.strip("/")
        ):
            return value
    return None


@dataclass(frozen=True)
class Outcome:
    state: str
    message: str = ""
    url: str | None = None
    post_id: str | None = None


def outcome_from_result(result: dict, platform: str, *, history: bool = False) -> Outcome:
    """Only explicit per-platform evidence can establish a completed publication."""
    status = result.get("status")
    message = str(result.get("error_message") or result.get("error") or "")
    if result.get("fallback_to_inbox") is True:
        return Outcome("needs_action", "Delivered to TikTok inbox; not publicly published.")
    if result.get("skipped") is True or status == "skipped":
        return Outcome("failed", "Platform was skipped; reconnect the account in Upload-Post.")
    if status in {"queued", "pending", "processing", "in_progress", "retryable"}:
        return Outcome("pending", "Provider is processing this destination.")
    if status == "failed" or result.get("success") is False:
        return Outcome("failed", message or "Provider reported a platform failure.")
    url = public_url(result.get("post_url") or result.get("url"), platform)
    identifier = result.get("platform_post_id") or result.get("post_id") or result.get("video_id")
    if status == "completed" or (result.get("success") is True and (history or url)):
        if result.get("warnings"):
            # In particular, ignored privacy/declaration fields must not be called a clean success.
            return Outcome(
                "needs_action",
                "Provider warning; inspect the published post: " + str(result["warnings"]),
                url,
                str(identifier) if identifier else None,
            )
        return Outcome(
            "published",
            "Published; link pending." if not url else "Published.",
            url,
            str(identifier) if identifier else None,
        )
    return Outcome("pending", "Awaiting explicit publication confirmation.")


def result_list(payload: dict, key: str = "results") -> list[dict]:
    results = payload.get(key, [])
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    if isinstance(results, dict):
        return [
            dict(item, platform=platform)
            for platform, item in results.items()
            if isinstance(item, dict)
        ]
    return []
