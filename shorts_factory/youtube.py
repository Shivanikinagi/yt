"""YouTube Data API v3 publishing with OAuth 2.0 (installed app), resumable uploads, quota tracking."""
from __future__ import annotations

import base64
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .scripting import VideoScript
from .state import State
from .utils import SECRETS_DIR, PipelineError, env_flag

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILE = SECRETS_DIR / "youtube_token.json"
CLIENT_FILE = SECRETS_DIR / "youtube_client_secret.json"


def _load_env_json(name: str) -> dict | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as e:
            raise PipelineError(f"{name} is neither JSON nor base64 JSON") from e


def client_config() -> dict | None:
    data = _load_env_json("YOUTUBE_CLIENT_SECRET_JSON")
    if data is None and CLIENT_FILE.exists():
        data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
    return data


def load_credentials(logger):
    """Return refreshed google Credentials or None when not configured."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.warning("google client libraries not installed; uploads disabled")
        return None
    info = _load_env_json("YOUTUBE_TOKEN_JSON")
    if info is None and TOKEN_FILE.exists():
        info = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if not info:
        return None
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if TOKEN_FILE.parent.exists() and not os.environ.get("CI"):
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise PipelineError("YouTube credentials invalid and not refreshable; run `auth` again")
    return creds


def run_auth_flow(logger) -> Path:
    """Interactive one-time authorization. Opens a browser, stores the refresh token locally."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    cfg = client_config()
    if not cfg:
        raise PipelineError(f"No OAuth client. Save the Google Cloud OAuth client JSON as {CLIENT_FILE} "
                            "or set YOUTUBE_CLIENT_SECRET_JSON.")
    flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    logger.info("saved %s", TOKEN_FILE)
    b64 = base64.b64encode(creds.to_json().encode("utf-8")).decode("ascii")
    print("\nAdd this as the GitHub secret YOUTUBE_TOKEN_JSON (base64 of the token file):\n")
    print(b64)
    print()
    return TOKEN_FILE


def build_service(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ----------------------------------------------------------------------------- scheduling
def next_publish_slot(cfg, state: State, now_utc: datetime | None = None) -> datetime:
    tz = ZoneInfo(cfg.timezone)
    now_utc = now_utc or datetime.now(timezone.utc)
    earliest = now_utc + timedelta(minutes=int(cfg.youtube.schedule_min_lead_minutes))
    booked = state.booked_publish_times()
    local_now = now_utc.astimezone(tz)
    for day in range(0, 21):
        d = (local_now + timedelta(days=day)).date()
        for slot in cfg.youtube.schedule_slots_local:
            hh, mm = [int(x) for x in slot.split(":")]
            cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            iso = cand.strftime("%Y-%m-%dT%H:%M:%SZ")
            if cand >= earliest and iso not in booked:
                return cand
    raise PipelineError("no free publish slot in the next 3 weeks")


def effective_publish_mode(cfg, logger) -> str:
    mode = str(cfg.youtube.publish_mode).lower()
    if mode == "scheduled" and not env_flag("ALLOW_PUBLIC_SCHEDULING"):
        logger.warning("publish_mode=scheduled but ALLOW_PUBLIC_SCHEDULING is not true -> uploading as private")
        return "private"
    if mode not in ("private", "unlisted", "scheduled"):
        return "private"
    return mode


def build_metadata(script: VideoScript, cfg, mode: str, publish_at: datetime | None) -> dict:
    title = script.title
    if "#shorts" not in title.lower() and len(title) + 8 <= 100:
        title = f"{title} #Shorts"
    sources = "\n".join(f"- {u}" for u in script.sources)
    desc = (f"{script.description}\n\nSources:\n{sources}\n\n{cfg.youtube.description_footer.strip()}\n\n"
            f"{' '.join(script.hashtags)}")
    tags = [h.lstrip("#") for h in script.hashtags] + ["shorts", "technology", "explained"]
    body = {
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:30],
                    "categoryId": str(cfg.youtube.category_id),
                    "defaultLanguage": cfg.youtube.default_language,
                    "defaultAudioLanguage": cfg.youtube.default_language},
        "status": {"privacyStatus": "unlisted" if mode == "unlisted" else "private",
                   "selfDeclaredMadeForKids": bool(cfg.youtube.made_for_kids),
                   "license": "youtube", "embeddable": True},
    }
    if mode == "scheduled" and publish_at is not None:
        body["status"]["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if cfg.youtube.declare_synthetic_media:
        body["status"]["containsSyntheticMedia"] = True
    return body


# ----------------------------------------------------------------------------- upload
def upload_video(service, video: dict, script: VideoScript, cfg, state: State, logger, date: str) -> dict:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    cost = int(cfg.youtube.upload_cost_units)
    if state.quota_used(date) + cost > int(cfg.youtube.daily_quota_budget):
        return {"skipped": f"quota budget reached ({state.quota_used(date)} used)"}
    mode = effective_publish_mode(cfg, logger)
    publish_at = next_publish_slot(cfg, state) if mode == "scheduled" else None
    body = build_metadata(script, cfg, mode, publish_at)
    path = Path(video["path"])

    def do_upload(meta: dict) -> dict:
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = service.videos().insert(part="snippet,status", body=meta, media_body=media)
        response, attempt = None, 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    logger.info("  upload %d%%", int(status.progress() * 100))
            except HttpError as e:
                if e.resp.status in (500, 502, 503, 504, 429) and attempt < 6:
                    attempt += 1
                    wait = min(2 ** attempt + random.random(), 60)
                    logger.warning("  retriable HTTP %s, retry %d in %.0fs", e.resp.status, attempt, wait)
                    time.sleep(wait)
                    continue
                raise
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempt < 6:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 60))
                    continue
                raise PipelineError(f"upload network failure: {e}") from e
        return response

    state.add_quota(date, cost)  # counted even if the request fails: YouTube bills attempts
    try:
        response = do_upload(body)
    except HttpError as e:
        text = e.content.decode("utf-8", "replace") if hasattr(e, "content") else str(e)
        if "containsSyntheticMedia" in text and "containsSyntheticMedia" in body["status"]:
            logger.warning("API rejected containsSyntheticMedia; retrying without it (declare it in Studio)")
            body["status"].pop("containsSyntheticMedia")
            state.add_quota(date, cost)
            response = do_upload(body)
        else:
            raise PipelineError(f"YouTube upload failed: {text[:400]}") from e
    vid = response["id"]
    result = {"youtube_id": vid, "url": f"https://youtube.com/shorts/{vid}",
              "privacy": response.get("status", {}).get("privacyStatus"),
              "publish_at": body["status"].get("publishAt"), "mode": mode}
    if cfg.youtube.upload_captions and video.get("srt") and Path(video["srt"]).exists():
        try:
            state.add_quota(date, 400)
            service.captions().insert(part="snippet", body={"snippet": {"videoId": vid, "language": cfg.youtube.default_language,
                                                                        "name": "English (auto-generated by pipeline)"}},
                                      media_body=MediaFileUpload(video["srt"], mimetype="application/octet-stream")).execute()
            result["captions_uploaded"] = True
        except Exception as e:  # not fatal
            logger.warning("caption upload failed: %s", str(e)[:200])
    return result


def verify_video(service, youtube_id: str, state: State, date: str) -> dict:
    state.add_quota(date, 1)
    resp = service.videos().list(part="status,processingDetails", id=youtube_id).execute()
    items = resp.get("items") or []
    if not items:
        return {"found": False}
    st = items[0].get("status", {})
    pd = items[0].get("processingDetails", {})
    return {"found": True, "privacy": st.get("privacyStatus"), "upload": st.get("uploadStatus"),
            "publish_at": st.get("publishAt"), "processing": pd.get("processingStatus")}
