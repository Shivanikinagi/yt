"""Persistent job state in a small JSON file (committed back to the repo by the cloud runner).

Tracks topics already used (duplicate prevention), every video produced with its upload status,
run history and YouTube API quota consumption per day.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .utils import STATE_DIR, read_json, write_json

STATE_FILE = STATE_DIR / "state.json"


class State:
    def __init__(self, path: Path = STATE_FILE):
        self.path = Path(path)
        self.data = read_json(self.path, default=None) or {
            "version": 1, "topics": {}, "videos": [], "runs": [], "quota": {}, "schedule": []}

    # --------------------------------------------------------------- persistence
    def save(self) -> None:
        write_json(self.path, self.data)

    # --------------------------------------------------------------- topics
    def topic_used(self, topic_id: str) -> bool:
        return topic_id in self.data["topics"]

    def mark_topic(self, topic_id: str, title: str, url: str, date: str) -> None:
        self.data["topics"][topic_id] = {"title": title, "url": url, "used_on": date}

    def recent_titles(self, days: int) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        titles = [t["title"] for t in self.data["topics"].values() if t.get("used_on", "") >= cutoff]
        titles += [v["title"] for v in self.data["videos"] if v.get("date", "") >= cutoff]
        return titles

    def similar_title(self, title: str, days: int, threshold: float) -> str | None:
        norm = title.lower().strip()
        for other in self.recent_titles(days):
            if difflib.SequenceMatcher(None, norm, other.lower().strip()).ratio() >= threshold:
                return other
        return None

    # --------------------------------------------------------------- videos
    def add_video(self, record: dict) -> None:
        self.data["videos"].append(record)

    def get_video(self, video_id: str) -> dict | None:
        for v in self.data["videos"]:
            if v["id"] == video_id:
                return v
        return None

    def update_video(self, video_id: str, **fields) -> None:
        v = self.get_video(video_id)
        if v is not None:
            v.update(fields)

    def videos_for_date(self, date: str) -> list[dict]:
        return [v for v in self.data["videos"] if v.get("date") == date]

    def pending_uploads(self, include_demo: bool = False) -> list[dict]:
        return [v for v in self.data["videos"]
                if v.get("status") == "rendered" and not v.get("youtube_id") and Path(v.get("path", "")).exists()
                and (include_demo or not v.get("demo"))]

    # --------------------------------------------------------------- quota
    def quota_used(self, date: str) -> int:
        return int(self.data["quota"].get(date, 0))

    def add_quota(self, date: str, units: int) -> None:
        self.data["quota"][date] = self.quota_used(date) + int(units)

    # --------------------------------------------------------------- schedule slots
    def booked_publish_times(self) -> set[str]:
        return {v["publish_at"] for v in self.data["videos"] if v.get("publish_at")}

    # --------------------------------------------------------------- runs
    def add_run(self, record: dict) -> None:
        self.data["runs"].append(record)
        self.data["runs"] = self.data["runs"][-200:]
