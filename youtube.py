#!/usr/bin/env python3
"""
Репостер YouTube -> Telegram.

Берёт новые видео канала через открытый RSS-фид (без yt-dlp и без логина),
отсеивает Shorts и постит в Telegram: превью, жирный заголовок,
кусок описания и ссылка «Дивитись зараз».

Формат YT_CHANNELS:  @ник_канала:@телеграм_канал, @ник2:@канал2
Состояние — в state_youtube.json (отдельно от TikTok-бота).
"""

import html
import json
import os
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Маркеры для вырезания куска описания:
# берём текст ПОСЛЕ START_MARKER и ДО строки с END_MARKER.
START_MARKER = os.environ.get("DESC_START", "on air.")
END_MARKER = os.environ.get("DESC_END", "у соціальних мережах")
CTA_TEXT = os.environ.get("CTA_TEXT", "Дивитись зараз")

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SKIP_SHORTS = os.environ.get("SKIP_SHORTS", "1") == "1"

STATE_FILE = pathlib.Path("state_youtube.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_CAPTION_LIMIT = 1024

HASHTAG_RE = re.compile(r"#\w+")
CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[\w-]{20,})"')

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def parse_channels(raw: str) -> list:
    """Разбирает '@ютуб:@телеграм, ...' в список пар."""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        yt, tg = item.split(":", 1)
        yt, tg = yt.strip().lstrip("@"), tg.strip()
        if yt and tg:
            result.append((yt, tg))
    return result


CHANNELS = parse_channels(os.environ.get("YT_CHANNELS", ""))


def extract_description(desc: str) -> str:
    """Вырезает содержательный кусок описания между маркерами."""
    text = desc or ""

    idx = text.find(START_MARKER)
    if idx != -1:
        text = text[idx + len(START_MARKER):]
    else:
        alt = text.find("В ефірі каналу")
        if alt != -1:
            nl = text.find("\n", alt)
            text = text[nl:] if nl != -1 else text[alt:]

    end = text.find(END_MARKER)
    if end != -1:
        line_start = text.rfind("\n", 0, end)
        text = text[: line_start if line_start != -1 else end]

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


def resolve_channel_id(handle: str) -> str:
    """Превращает @ник в внутренний id канала (UC...), нужный для RSS."""
    if handle.startswith("UC") and len(handle) > 20:
        return handle          # уже готовый id
    r = requests.get(
        f"https://www.youtube.com/@{handle}", headers=HEADERS, timeout=30
    )
    r.raise_for_status()
    m = CHANNEL_ID_RE.search(r.text)
    if not m:
        raise RuntimeError("не нашёл channelId на странице канала")
    return m.group(1)


def fetch_feed(channel_id: str) -> list:
    """Последние ~15 видео канала из RSS. Новые идут первыми."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    items = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", "", NS)
        if not vid:
            continue
        group = entry.find("media:group", NS)
        desc = group.findtext("media:description", "", NS) if group is not None else ""
        thumb = ""
        if group is not None:
            t = group.find("media:thumbnail", NS)
            if t is not None:
                thumb = t.get("url") or ""
        items.append(
            {
                "id": vid,
                "title": (entry.findtext("atom:title", "", NS) or "").strip(),
                "description": desc or "",
                "thumb": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return items


def is_short(video_id: str) -> bool:
    """Shorts отдают 200 по адресу /shorts/<id>, обычные видео — редирект."""
    try:
        r = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            headers=HEADERS,
            allow_redirects=False,
            timeout=20,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  ~ проверка на Shorts не удалась ({e}), считаем обычным видео")
        return False


def build_caption(title: str, desc: str, url: str) -> str:
    """Собирает подпись в HTML с учётом лимита Telegram."""
    separators = 4
    room = TG_CAPTION_LIMIT - len(title) - len(CTA_TEXT) - separators

    body = (desc or "").strip()
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"

    parts = [f"<b>{html.escape(title)}</b>"]
    if body:
        parts.append(html.escape(body))
    parts.append(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(CTA_TEXT)}</a>'
    )
    return "\n\n".join(parts)


def send_post(tg_channel: str, thumb: str, caption: str) -> bool:
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

    # запасной путь: текстом (превью подтянет сама телега по ссылке)
    r2 = requests.post(
        f"{TG_API}/sendMessage",
        data={"chat_id": tg_channel, "text": caption, "parse_mode": "HTML"},
        timeout=60,
    )
    ok = r2.ok and r2.json().get("ok")
    if not ok:
        print(f"  ! sendMessage тоже не прошёл: {r2.text[:250]}", file=sys.stderr)
    return bool(ok)


def process_channel(handle: str, tg_channel: str, state: dict) -> None:
    try:
        channel_id = resolve_channel_id(handle)
        videos = fetch_feed(channel_id)
    except Exception as e:
        print(f"[{handle}] не удалось получить фид: {e}", file=sys.stderr)
        return

    if not videos:
        print(f"[{handle}] фид пуст")
        return

    if handle not in state:
        state[handle] = [v["id"] for v in videos]
        print(f"[{handle}] первый запуск — засеяли {len(videos)}, посты не шлём")
        return

    posted = set(state.get(handle, []))
    new = [v for v in videos if v["id"] not in posted]
    new = list(reversed(new))[:MAX_PER_RUN]
    if not new:
        print(f"[{handle}] новых видео нет")
        return

    for v in new:
        if SKIP_SHORTS and is_short(v["id"]):
            print(f"[{handle}] {v['id']} — Shorts, пропускаем")
            state.setdefault(handle, []).append(v["id"])
            save_state(state)
            continue

        print(f"[{handle}] новое видео {v['id']} -> {tg_channel}")
        caption = build_caption(
            v["title"], extract_description(v["description"]), v["url"]
        )
        if send_post(tg_channel, v["thumb"], caption):
            state.setdefault(handle, []).append(v["id"])
            save_state(state)


def main() -> None:
    if not CHANNELS:
        print("YT_CHANNELS не задан — нечего делать")
        return
    state = load_state()
    for handle, tg_channel in CHANNELS:
        process_channel(handle, tg_channel, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
