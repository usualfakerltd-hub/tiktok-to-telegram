#!/usr/bin/env python3
"""
Репостер YouTube -> Telegram.

Берёт новые видео с вкладки /videos канала (Shorts туда не попадают)
и постит в Telegram: превью-картинка, жирный заголовок, кусок описания
и кнопка-ссылка «Дивитись зараз».

Формат YT_CHANNELS:  @ник_канала:@телеграм_канал, @ник2:@канал2
Состояние хранится в state_youtube.json (отдельно от TikTok-бота).
"""

import html
import json
import os
import pathlib
import re
import sys

import requests
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Маркеры для вырезания куска описания.
# Берём текст ПОСЛЕ START_MARKER и ДО строки с END_MARKER.
START_MARKER = os.environ.get("DESC_START", "on air.")
END_MARKER = os.environ.get("DESC_END", "у соціальних мережах")
CTA_TEXT = os.environ.get("CTA_TEXT", "Дивитись зараз")

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))

STATE_FILE = pathlib.Path("state_youtube.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_CAPTION_LIMIT = 1024

HASHTAG_RE = re.compile(r"#\w+")


def parse_channels(raw: str) -> list:
    """Разбирает '@ютуб:@телеграм, ...' в список пар."""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        yt, tg = item.split(":", 1)
        yt = yt.strip().lstrip("@")
        tg = tg.strip()
        if yt and tg:
            result.append((yt, tg))
    return result


CHANNELS = parse_channels(os.environ.get("YT_CHANNELS", ""))


def extract_description(desc: str) -> str:
    """Вырезает содержательный кусок описания между маркерами."""
    text = desc or ""

    # 1) отрезаем всё до маркера начала (там обычно реклама)
    idx = text.find(START_MARKER)
    if idx != -1:
        text = text[idx + len(START_MARKER):]
    else:
        alt = text.find("В ефірі каналу")
        if alt != -1:
            nl = text.find("\n", alt)
            text = text[nl:] if nl != -1 else text[alt:]

    # 2) отрезаем всё от маркера конца (контакты, соцсети)
    end = text.find(END_MARKER)
    if end != -1:
        line_start = text.rfind("\n", 0, end)
        text = text[: line_start if line_start != -1 else end]

    # 3) чистим хэштеги и то, что после них осталось
    text = HASHTAG_RE.sub("", text)
    text = re.sub(r"[ \t]+([,.:;!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_videos(channel: str) -> list:
    """Видео с вкладки /videos — Shorts туда не попадают."""
    url = f"https://www.youtube.com/@{channel}/videos"
    opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
        "playlistend": 15,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    return [
        {"id": str(e.get("id")), "url": f"https://www.youtube.com/watch?v={e.get('id')}"}
        for e in entries
        if e.get("id")
    ]


def get_details(url: str) -> dict:
    """Метаданные одного видео: заголовок, описание, превью."""
    opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    vid = info.get("id")
    return {
        "title": (info.get("title") or "").strip(),
        "description": info.get("description") or "",
        "thumb": info.get("thumbnail")
        or f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        "duration": info.get("duration") or 0,
    }


def build_caption(title: str, desc: str, url: str) -> str:
    """Собирает подпись в HTML с учётом лимита Telegram."""
    # Telegram считает лимит по видимому тексту, а не по HTML-разметке,
    # поэтому бюджет считаем на неэкранированных строках.
    separators = 4          # два разделителя "\n\n"
    room = TG_CAPTION_LIMIT - len(title) - len(CTA_TEXT) - separators

    body = desc.strip()
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"

    parts = [f"<b>{html.escape(title)}</b>"]
    if body:
        parts.append(html.escape(body))
    parts.append(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(CTA_TEXT)}</a>'
    )
    return "\n\n".join(parts)


def send_post(tg_channel: str, thumb: str, caption: str, url: str) -> bool:
    r = requests.post(
        f"{TG_API}/sendPhoto",
        data={
            "chat_id": tg_channel,
            "photo": thumb,
            "caption": caption,
            "parse_mode": "HTML",
        },
        timeout=90,
    )
    if r.ok and r.json().get("ok"):
        return True

    print(f"  ! sendPhoto не прошёл: {r.text[:250]}", file=sys.stderr)

    # запасной путь: текстом со ссылкой (превью подтянет сама телега)
    r2 = requests.post(
        f"{TG_API}/sendMessage",
        data={"chat_id": tg_channel, "text": caption, "parse_mode": "HTML"},
        timeout=60,
    )
    ok = r2.ok and r2.json().get("ok")
    if not ok:
        print(f"  ! sendMessage тоже не прошёл: {r2.text[:250]}", file=sys.stderr)
    return bool(ok)


def process_channel(yt_channel: str, tg_channel: str, state: dict) -> None:
    posted = set(state.get(yt_channel, []))
    try:
        videos = list_videos(yt_channel)
    except Exception as e:
        print(f"[{yt_channel}] не удалось получить список: {e}", file=sys.stderr)
        return

    if not videos:
        print(f"[{yt_channel}] список пуст")
        return

    if yt_channel not in state:
        state[yt_channel] = [v["id"] for v in videos]
        print(f"[{yt_channel}] первый запуск — засеяли {len(videos)}, посты не шлём")
        return

    new = [v for v in videos if v["id"] not in posted]
    new = list(reversed(new))[:MAX_PER_RUN]
    if not new:
        print(f"[{yt_channel}] новых видео нет")
        return

    for v in new:
        print(f"[{yt_channel}] новое видео {v['id']} -> {tg_channel}")
        try:
            d = get_details(v["url"])
        except Exception as e:
            print(f"  ! метаданные не получены: {e}", file=sys.stderr)
            continue

        desc = extract_description(d["description"])
        caption = build_caption(d["title"], desc, v["url"])

        if send_post(tg_channel, d["thumb"], caption, v["url"]):
            state.setdefault(yt_channel, []).append(v["id"])
            save_state(state)


def main() -> None:
    if not CHANNELS:
        print("YT_CHANNELS не задан — нечего делать")
        return
    state = load_state()
    for yt_channel, tg_channel in CHANNELS:
        process_channel(yt_channel, tg_channel, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
