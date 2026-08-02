#!/usr/bin/env python3
"""
Репостер TikTok -> Telegram.
Каждому TikTok-аккаунту можно задать свой Telegram-канал.
Формат TIKTOK_USERS:  ник1:@канал1, ник2:@канал2
Если канал не указан — используется CHANNEL_ID.
"""

import json
import os
import pathlib
import re
import sys
import tempfile

import requests
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_CHANNEL = os.environ.get("CHANNEL_ID", "")


def parse_users(raw: str) -> list:
    """Разбирает 'ник1:@канал1, ник2' в список пар (ник, канал)."""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            user, chan = item.split(":", 1)
            user, chan = user.strip().lstrip("@"), chan.strip()
        else:
            user, chan = item.lstrip("@"), DEFAULT_CHANNEL
        if not chan:
            print(f"[{user}] не задан канал — пропускаем", file=sys.stderr)
            continue
        result.append((user, chan))
    return result


USERS = parse_users(os.environ["TIKTOK_USERS"])

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))

STATE_FILE = pathlib.Path("state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_VIDEO_LIMIT = 50 * 1024 * 1024
TG_CAPTION_LIMIT = 1024

HASHTAG_RE = re.compile(r"#[^\s#]+")


def clean_caption(text: str) -> str:
    """Убирает хэштеги и приводит в порядок то, что осталось."""
    text = HASHTAG_RE.sub("", text or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > TG_CAPTION_LIMIT:
        text = text[: TG_CAPTION_LIMIT - 1].rstrip() + "…"
    return text


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_videos(user: str) -> list:
    url = f"https://www.tiktok.com/@{user}"
    opts = {"extract_flat": True, "quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    videos = []
    for e in entries:
        vid = str(e.get("id"))
        link = e.get("url") or f"https://www.tiktok.com/@{user}/video/{vid}"
        videos.append({"id": vid, "url": link})
    return videos


def download(url: str):
    tmp = tempfile.mkdtemp()
    opts = {
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "format": "mp4/best",
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    raw = info.get("description") or info.get("title") or ""
    return path, clean_caption(raw)


def send_video(path: str, caption: str, channel: str) -> bool:
    with open(path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendVideo",
            data={"chat_id": channel, "caption": caption, "supports_streaming": True},
            files={"video": f},
            timeout=180,
        )
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! Telegram отклонил: {r.text[:300]}", file=sys.stderr)
    return bool(ok)


def process_user(user: str, channel: str, state: dict) -> None:
    posted = set(state.get(user, []))
    try:
        videos = list_videos(user)
    except Exception as e:
        print(f"[{user}] не удалось получить список: {e}", file=sys.stderr)
        return

    if not videos:
        print(f"[{user}] список пуст (возможно, TikTok ограничил IP)")
        return

    if user not in state:
        state[user] = [v["id"] for v in videos]
        print(f"[{user}] первый запуск — засеяли {len(videos)} видео, посты не шлём")
        return

    new = [v for v in videos if v["id"] not in posted]
    new = list(reversed(new))[:MAX_PER_RUN]
    if not new:
        print(f"[{user}] новых видео нет")
        return

    for v in new:
        print(f"[{user}] новое видео {v['id']} -> {channel}")
        try:
            path, caption = download(v["url"])
        except Exception as e:
            print(f"  ! не скачалось: {e}", file=sys.stderr)
            continue

        size = os.path.getsize(path)
        if size > TG_VIDEO_LIMIT:
            print(f"  ! слишком большое ({size // 1024 // 1024} МБ) — пропускаем")
            state.setdefault(user, []).append(v["id"])
            save_state(state)
            os.remove(path)
            continue

        if send_video(path, caption, channel):
            state.setdefault(user, []).append(v["id"])
            save_state(state)
        os.remove(path)


def main() -> None:
    state = load_state()
    for user, channel in USERS:
        process_user(user, channel, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
