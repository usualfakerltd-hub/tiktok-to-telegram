#!/usr/bin/env python3
"""
Репостер TikTok -> Telegram.

Тянет новые видео с указанных TikTok-аккаунтов и постит их в Telegram-канал.
Запускается по расписанию через GitHub Actions, состояние (какие видео уже
отправлены) хранит в файле state.json прямо в репозитории.
"""

import json
import os
import pathlib
import sys
import tempfile

import requests
import yt_dlp

# --- конфиг из переменных окружения (задаются в GitHub -> Secrets) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]        # токен бота от @BotFather
CHANNEL_ID = os.environ["CHANNEL_ID"]      # @имя_канала (публичный) или -100xxxxxxxxxx
USERS = [                                  # один или несколько ников через запятую
    u.strip().lstrip("@")
    for u in os.environ["TIKTOK_USERS"].split(",")
    if u.strip()
]

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))   # максимум видео за один запуск
STATE_FILE = pathlib.Path("state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_VIDEO_LIMIT = 50 * 1024 * 1024          # лимит бота на sendVideo ~50 МБ


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}   # файла нет -> самый первый запуск


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_videos(user: str) -> list[dict]:
    """Список видео пользователя, новые сверху. Без скачивания — только id и ссылка."""
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


def download(url: str) -> tuple[str, str]:
    """Скачивает видео без вотермарки. Возвращает (путь_к_файлу, описание)."""
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
    caption = (info.get("description") or "").strip()
    return path, caption


def send_video(path: str, caption: str, source_url: str) -> bool:
    text = caption[:900]
    if text:
        text += "\n\n"
    text += source_url
    with open(path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendVideo",
            data={"chat_id": CHANNEL_ID, "caption": text, "supports_streaming": True},
            files={"video": f},
            timeout=180,
        )
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! Telegram отклонил: {r.text[:300]}", file=sys.stderr)
    return bool(ok)


def process_user(user: str, state: dict) -> None:
    posted = set(state.get(user, []))
    try:
        videos = list_videos(user)
    except Exception as e:
        print(f"[{user}] не удалось получить список: {e}", file=sys.stderr)
        return

    if not videos:
        print(f"[{user}] список пуст (возможно, TikTok ограничил IP раннера)")
        return

    # Первый запуск для этого ника: помечаем всё текущее как отправленное
    # и НИЧЕГО не постим, чтобы не завалить канал старым архивом.
    if user not in state:
        state[user] = [v["id"] for v in videos]
        print(f"[{user}] первый запуск — засеяли {len(videos)} видео, посты не шлём")
        return

    # Новые = те, которых нет в отправленных. Постим от старых к новым.
    new = [v for v in videos if v["id"] not in posted]
    new = list(reversed(new))[:MAX_PER_RUN]
    if not new:
        print(f"[{user}] новых видео нет")
        return

    for v in new:
        print(f"[{user}] новое видео {v['id']}")
        try:
            path, caption = download(v["url"])
        except Exception as e:
            print(f"  ! не скачалось: {e}", file=sys.stderr)
            continue

        size = os.path.getsize(path)
        if size > TG_VIDEO_LIMIT:
            print(f"  ! слишком большое ({size // 1024 // 1024} МБ) — шлём ссылкой")
            requests.post(
                f"{TG_API}/sendMessage",
                data={"chat_id": CHANNEL_ID, "text": v["url"]},
                timeout=60,
            )
            state.setdefault(user, []).append(v["id"])
            save_state(state)
            os.remove(path)
            continue

        if send_video(path, caption, v["url"]):
            state.setdefault(user, []).append(v["id"])
            save_state(state)   # сохраняем сразу — чтобы при сбое не задублировать
        os.remove(path)


def main() -> None:
    state = load_state()
    for user in USERS:
        process_user(user, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
