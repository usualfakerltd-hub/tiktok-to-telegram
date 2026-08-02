#!/usr/bin/env python3
"""
Репостер YouTube -> Telegram.

Берёт новые видео канала через открытый RSS-фид (без yt-dlp и без логина),
отсеивает Shorts и старые ролики, постит в Telegram: превью, жирный
заголовок, кусок описания и ссылка «Дивитись зараз».

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
from datetime import datetime, timedelta, timezone

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Маркеры: берём текст ПОСЛЕ начального и ДО строки с конечным.
# Можно перечислить несколько через | (вертикальную черту).
START_MARKERS = os.environ.get("DESC_START", "on air.|В ефірі каналу").split("|")
END_MARKERS = os.environ.get(
    "DESC_END",
    "у соціальних мережах|в соцсетях|Ще більше про футбол|Еще больше про футбол"
    "|Посилання на це відео|Ссылка на этот",
).split("|")
CTA_TEXT = os.environ.get("CTA_TEXT", "ДИВИТИСЬ ЗАРАЗ")
CTA_EMOJI = os.environ.get("CTA_EMOJI", "▶️")

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SKIP_SHORTS = os.environ.get("SKIP_SHORTS", "1") == "1"
# Страховка: ролики старше этого числа дней не постим никогда.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))

STATE_FILE = pathlib.Path("state_youtube.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_CAPTION_LIMIT = 1024

HASHTAG_RE = re.compile(r"#\w+")
RSS_LINK_RE = re.compile(
    r'href="(https://www\.youtube\.com/feeds/videos\.xml\?channel_id=UC[\w-]+)"'
)
EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[\w-]{20,})"')

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

    # 1) отрезаем всё до маркера начала (там обычно реклама)
    for marker in START_MARKERS:
        marker = marker.strip()
        if not marker:
            continue
        idx = text.find(marker)
        if idx != -1:
            cut = idx + len(marker)
            # если маркер — начало строки, режем до конца этой строки
            nl = text.find("\n", cut)
            rest = text[cut:]
            if rest.split("\n", 1)[0].strip() and marker == "В ефірі каналу":
                rest = text[nl:] if nl != -1 else rest
            text = rest
            break

    # 2) отрезаем всё от маркера конца (контакты, соцсети, ссылки)
    cut_at = len(text)
    for marker in END_MARKERS:
        marker = marker.strip()
        if not marker:
            continue
        pos = text.find(marker)
        if pos != -1:
            line_start = text.rfind("\n", 0, pos)
            cut_at = min(cut_at, line_start if line_start != -1 else pos)
    text = text[:cut_at]

    # 3) чистим хэштеги, ссылки и мусор
    text = HASHTAG_RE.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
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


def find_feed_url(handle: str) -> str:
    """Ищет адрес RSS канала — берём его прямо со страницы канала."""
    r = requests.get(
        f"https://www.youtube.com/@{handle}", headers=HEADERS, timeout=30
    )
    r.raise_for_status()
    page = r.text

    m = RSS_LINK_RE.search(page)
    if m:
        return m.group(1)

    m = EXTERNAL_ID_RE.search(page)
    if m:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"

    raise RuntimeError("не нашёл адрес RSS на странице канала")


def fetch_feed(feed_url: str) -> list:
    """Последние ~15 видео канала. Новые идут первыми."""
    r = requests.get(feed_url, headers=HEADERS, timeout=30)
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
                "published": entry.findtext("atom:published", "", NS) or "",
                "thumb": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return items


def is_too_old(published: str) -> bool:
    """True, если ролик старше MAX_AGE_DAYS. Защита от заливки архива."""
    if not published:
        return False
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt) > timedelta(days=MAX_AGE_DAYS)


def best_thumb(video_id: str, fallback: str) -> str:
    """Подбирает превью максимального качества. maxres/hq720 идут 16:9 без полос."""
    for name in ("maxresdefault.jpg", "hq720.jpg", "sddefault.jpg"):
        url = f"https://i.ytimg.com/vi/{video_id}/{name}"
        try:
            r = requests.head(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return fallback


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


def paragraphize(text: str) -> str:
    """Разбивает описание на 2-4 абзаца в зависимости от объёма."""
    if not text:
        return ""

    # если автор уже разбил на 2-4 абзаца — оставляем как есть
    existing = [b.strip() for b in text.split("\n\n") if b.strip()]
    if 2 <= len(existing) <= 4:
        return "\n\n".join(existing)

    lines = [ln.strip() for ln in text.replace("\n\n", "\n").split("\n") if ln.strip()]
    if len(lines) <= 2:
        return "\n".join(lines)

    total = sum(len(ln) for ln in lines)
    if total < 250:
        n = 2
    elif total < 550:
        n = 3
    else:
        n = 4
    n = min(n, len(lines))

    base, extra = divmod(len(lines), n)
    groups, i = [], 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        groups.append("\n".join(lines[i:i + size]))
        i += size
    return "\n\n".join(g for g in groups if g)


def build_caption(title: str, desc: str, url: str) -> str:
    separators = 4
    reserve = len(CTA_EMOJI) * 2 + 8      # эмодзи телега считает за 2 символа
    room = TG_CAPTION_LIMIT - len(title) - len(CTA_TEXT) - separators - reserve

    body = (desc or "").strip()
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"
    body = paragraphize(body)

    link = html.escape(url, quote=True)
    parts = [f'<b><a href="{link}">{html.escape(title)}</a></b>']
    if body:
        parts.append(html.escape(body))
    cta = f"{CTA_EMOJI} {CTA_TEXT}".strip()
    parts.append(f'<b><a href="{link}">{html.escape(cta)}</a></b>')
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
        feed_url = find_feed_url(handle)
        videos = fetch_feed(feed_url)
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
        if is_too_old(v["published"]):
            print(f"[{handle}] {v['id']} старее {MAX_AGE_DAYS} дн. — пропускаем")
            state.setdefault(handle, []).append(v["id"])
            save_state(state)
            continue

        if SKIP_SHORTS and is_short(v["id"]):
            print(f"[{handle}] {v['id']} — Shorts, пропускаем")
            state.setdefault(handle, []).append(v["id"])
            save_state(state)
            continue

        print(f"[{handle}] новое видео {v['id']} -> {tg_channel}")
        caption = build_caption(
            v["title"], extract_description(v["description"]), v["url"]
        )
        thumb = best_thumb(v["id"], v["thumb"])
        if send_post(tg_channel, thumb, caption):
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
