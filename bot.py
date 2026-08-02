#!/usr/bin/env python3
"""
Репостер TikTok -> Telegram.

Каждому TikTok-аккаунту можно задать свой Telegram-канал и своё
поведение с тегами.

Формат TIKTOK_USERS:
    ник:@канал              — поведение по умолчанию (из KEEP_TAGS)
    ник:@канал:tags         — теги ОСТАВИТЬ и сделать ссылками
    ник:@канал:notags       — теги ВЫРЕЗАТЬ

KEEP_TAGS=1 — по умолчанию оставлять теги, KEEP_TAGS=0 — вырезать.
"""

import html
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_CHANNEL = os.environ.get("CHANNEL_ID", "")
KEEP_TAGS_DEFAULT = os.environ.get("KEEP_TAGS", "0") == "1"

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
# Страховка: видео старше этого числа дней не постим никогда.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))
# Сколько последних id помнить по каждому аккаунту (чтобы файл не разрастался).
KEEP_HISTORY = int(os.environ.get("KEEP_HISTORY", "300"))

STATE_FILE = pathlib.Path("state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_VIDEO_LIMIT = 50 * 1024 * 1024
TG_CAPTION_LIMIT = 1024

HASHTAG_RE = re.compile(r"#\w+")
HASHTAG_CAP_RE = re.compile(r"#((?=\w*[^\W\d])\w+)")
MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_](?:[A-Za-z0-9_.]*[A-Za-z0-9_])?)")
# yt-dlp подставляет такой заголовок, когда описания нет — это не текст поста
PLACEHOLDER_RE = re.compile(r"^TikTok video #\d+$", re.IGNORECASE)


def parse_users(raw: str) -> list:
    """Разбирает 'ник:@канал:tags, ник2:@канал2' в список (ник, канал, теги)."""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue

        keep = KEEP_TAGS_DEFAULT
        parts = item.split(":")
        if len(parts) >= 3:
            flag = parts[2].strip().lower()
            if flag in ("tags", "keep"):
                keep = True
            elif flag in ("notags", "strip"):
                keep = False

        if len(parts) >= 2:
            user, chan = parts[0].strip().lstrip("@"), parts[1].strip()
        else:
            user, chan = parts[0].strip().lstrip("@"), DEFAULT_CHANNEL

        if not chan:
            print(f"[{user}] не задан канал — пропускаем", file=sys.stderr)
            continue
        result.append((user, chan, keep))
    return result


USERS = parse_users(os.environ["TIKTOK_USERS"])


def clean_caption(text: str, keep_tags: bool) -> str:
    """Чистит описание. Теги либо оставляет, либо вырезает."""
    text = text or ""
    if not keep_tags:
        text = HASHTAG_RE.sub("", text)
    text = re.sub(r"[ \t]+([,.:;!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > TG_CAPTION_LIMIT:
        text = text[: TG_CAPTION_LIMIT - 1].rstrip() + "…"
    return text


def linkify(escaped: str) -> str:
    """Делает ссылки из #тегов (на страницу тега) и @упоминаний (на аккаунт)."""

    def tag(m):
        word = m.group(1)
        return f'<a href="https://www.tiktok.com/tag/{quote(word)}">#{word}</a>'

    def mention(m):
        nick = m.group(1)
        return f'<a href="https://www.tiktok.com/@{quote(nick)}">@{nick}</a>'

    return MENTION_RE.sub(mention, HASHTAG_CAP_RE.sub(tag, escaped))


def is_too_old(video_id: str) -> bool:
    """В id видео TikTok зашита дата публикации (старшие 32 бита)."""
    try:
        ts = int(video_id) >> 32
        dt = datetime.fromtimestamp(ts, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return False
    if dt.year < 2016:
        return False          # id непохож на настоящий — не фильтруем
    return (datetime.now(timezone.utc) - dt) > timedelta(days=MAX_AGE_DAYS)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def remember(state: dict, user: str, video_id: str) -> None:
    """Кладёт id наверх списка и подрезает историю."""
    lst = state.setdefault(user, [])
    lst.insert(0, video_id)
    del lst[KEEP_HISTORY:]


def save_state(state: dict) -> None:
    """Пишет файл: аккаунты в обратном порядке добавления — новые сверху."""
    order = [u for u, _, _ in USERS]
    ordered = {}
    for user in reversed(order):
        if user in state:
            ordered[user] = state[user]
    for user in state:              # всё, чего нет в настройках, — в конец
        if user not in ordered:
            ordered[user] = state[user]

    STATE_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
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


def download(url: str, keep_tags: bool):
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
    raw = info.get("description") or ""
    if not raw.strip():
        title = (info.get("title") or "").strip()
        raw = "" if PLACEHOLDER_RE.match(title) else title
    return path, clean_caption(raw, keep_tags)


def send_video(path: str, caption: str, channel: str, keep_tags: bool) -> bool:
    data = {"chat_id": channel, "supports_streaming": True}
    if keep_tags:
        data["caption"] = linkify(html.escape(caption))
        data["parse_mode"] = "HTML"
    else:
        data["caption"] = caption

    with open(path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendVideo", data=data, files={"video": f}, timeout=180
        )
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! Telegram отклонил: {r.text[:300]}", file=sys.stderr)
    return bool(ok)


def process_user(user: str, channel: str, keep_tags: bool, state: dict) -> None:
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
        state[user] = [v["id"] for v in videos][:KEEP_HISTORY]
        print(f"[{user}] первый запуск — засеяли {len(videos)} видео, посты не шлём")
        return

    unseen = list(reversed([v for v in videos if v["id"] not in posted]))

    # Старьё отбраковываем пачкой сразу — оно не должно съедать лимит на посты.
    stale = [v for v in unseen if is_too_old(v["id"])]
    if stale:
        # В файл не пишем: возраст считается из самого id, запросов не требует.
        print(f"[{user}] старее {MAX_AGE_DAYS} дн. — пропущено: {len(stale)}")

    new = [v for v in unseen if not is_too_old(v["id"])][:MAX_PER_RUN]
    if not new:
        print(f"[{user}] новых видео нет")
        return

    for v in new:
        print(f"[{user}] новое видео {v['id']} -> {channel}")
        try:
            path, caption = download(v["url"], keep_tags)
        except Exception as e:
            print(f"  ! не скачалось: {e}", file=sys.stderr)
            continue

        size = os.path.getsize(path)
        if size > TG_VIDEO_LIMIT:
            print(f"  ! слишком большое ({size // 1024 // 1024} МБ) — пропускаем")
            remember(state, user, v["id"])
            save_state(state)
            os.remove(path)
            continue

        if send_video(path, caption, channel, keep_tags):
            remember(state, user, v["id"])
            save_state(state)
        os.remove(path)


def main() -> None:
    state = load_state()
    for user, channel, keep_tags in USERS:
        process_user(user, channel, keep_tags, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
