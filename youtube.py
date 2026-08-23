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
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import time
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
CTA_TEXT = os.environ.get("CTA_TEXT", "ДИВИТИСЬ ЗАРАЗ")          # ua
CTA_TEXT_RU = os.environ.get("CTA_TEXT_RU", "СМОТРЕТЬ СЕЙЧАС")   # ru
CTA_EMOJI = os.environ.get("CTA_EMOJI", "▶️")
# Режим описания: markers — резать по маркерам, first — только первый абзац,
# full — брать всё описание целиком.
DESC_MODE_DEFAULT = os.environ.get("DESC_MODE", "markers")

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SKIP_SHORTS = os.environ.get("SKIP_SHORTS", "1") == "1"
# Страховка: ролики старше этого числа дней не постим никогда.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))
# Shorts пытаться заливать файлом, а не карточкой
SHORTS_AS_VIDEO = os.environ.get("SHORTS_AS_VIDEO", "1") == "1"
# Необязательно: содержимое cookies.txt, если YouTube требует авторизацию
YT_COOKIES = os.environ.get("YT_COOKIES", "").strip()
# Ключ RapidAPI: запасной путь скачивания, когда YouTube блокирует IP раннера
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "youtube-video-and-shorts-downloader1.p.rapidapi.com"
)
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
TG_UPLOAD_LIMIT = 50 * 1024 * 1024

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
        parts = item.split(":")
        yt = parts[0].strip().lstrip("@")
        tg = parts[1].strip() if len(parts) > 1 else ""
        mode = parts[2].strip().lower() if len(parts) > 2 else DESC_MODE_DEFAULT
        if mode not in ("markers", "first", "full"):
            mode = DESC_MODE_DEFAULT
        # четвёртое поле: shorts — брать Shorts, noshorts — отсекать
        skip_shorts = SKIP_SHORTS
        if len(parts) > 3:
            flag = parts[3].strip().lower()
            if flag == "shorts":
                skip_shorts = False
            elif flag == "noshorts":
                skip_shorts = True
        # пятое поле: язык кнопки — ru или ua
        cta = CTA_TEXT
        if len(parts) > 4 and parts[4].strip().lower() == "ru":
            cta = CTA_TEXT_RU
        if yt and tg:
            result.append((yt, tg, mode, skip_shorts, cta))
    return result


CHANNELS = parse_channels(os.environ.get("YT_CHANNELS", ""))


def extract_description(desc: str, mode: str = "markers") -> str:
    """Достаёт нужный кусок описания в зависимости от режима канала."""
    text = desc or ""

    if mode == "first":
        # только первый абзац — до первой пустой строки
        for block in text.split("\n\n"):
            if block.strip():
                text = block
                break
        return _tidy(text)

    if mode == "full":
        return _tidy(text)

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
    return _tidy(text)


def _tidy(text: str) -> str:
    """Убирает хэштеги, ссылки и лишние пробелы."""
    text = HASHTAG_RE.sub("", text or "")
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

    blocked_markers = ("consent.youtube.com", "id=\"SB\"", "unusual traffic",
                        "confirm you're not a robot", "Enable JavaScript")
    if any(mk in page for mk in blocked_markers) or len(page) < 5000:
        print(f"  ~ страница канала подозрительно короткая/похожа на блок-страницу "
              f"(длина {len(page)} символов)")

    m = RSS_LINK_RE.search(page)
    if m:
        return m.group(1)

    m = EXTERNAL_ID_RE.search(page)
    if m:
        print(f"  ~ RSS-ссылка не найдена, беру channelId из externalId: {m.group(1)}")
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"

    raise RuntimeError(
        f"не нашёл адрес RSS на странице канала (status={r.status_code}, "
        f"длина страницы={len(page)})"
    )


def fetch_feed_with_retry(handle: str, attempts: int = 2) -> list:
    """Получает фид с одной повторной попыткой при неудаче — блокировки бывают разовыми."""
    last_err = None
    for i in range(attempts):
        try:
            feed_url = find_feed_url(handle)
            return fetch_feed(feed_url)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                print(f"  ~ попытка {i + 1} неудачна ({str(e)[:100]}), пробую снова")
                time.sleep(5)
    raise last_err


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


def best_thumb(video_id: str, fallback: str, vertical: bool = False) -> str:
    """Подбирает превью. Для Shorts сначала пробуем вертикальные варианты."""
    names = ("maxresdefault.jpg", "hq720.jpg", "sddefault.jpg")
    if vertical:
        # oar* — вертикальный кадр Shorts, без полей по бокам
        names = ("oardefault.jpg", "oar2.jpg", "frame0.jpg") + names
    for name in names:
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


def cookies_file() -> str:
    """Пишет cookies во временный файл, если они заданы."""
    if not YT_COOKIES:
        return ""
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(YT_COOKIES)
    return path


def _pick_stream(streams: list, want_audio: bool):
    """Выбирает лучшую дорожку: mp4, не выше MAX_HEIGHT, максимального качества."""
    best = None
    for st in streams:
        m = st.get("metadata") or {}
        if not st.get("url"):
            continue
        mime = m.get("mime_type") or ""
        if "mp4" not in mime:            # webm телега жуёт хуже
            continue
        if want_audio and not m.get("has_audio"):
            continue
        if m.get("has_video"):
            # у вертикальных Shorts 1080p — это 1080x1920, поэтому меряем
            # по короткой стороне, иначе всё отсеется как «слишком большое»
            res = min(m.get("width") or 0, m.get("height") or 0)
            if res > MAX_HEIGHT:
                continue
            key = res
        else:
            key = m.get("bitrate", 0)
        if best is None or key > best[0]:
            best = (key, st["url"], m)
    return best


def download_via_api(video_id: str):
    """Качает ролик через RapidAPI (у сервиса свои IP, YouTube их не режет)."""
    if not RAPIDAPI_KEY:
        print("  ~ RAPIDAPI_KEY не задан — пропускаю RapidAPI, пробую yt-dlp")
        return None, None

    try:
        r = requests.get(
            f"https://{RAPIDAPI_HOST}/youtube/v3/video/details",
            params={"videoId": video_id, "urlAccess": "proxied",
                    "getTranscript": "false"},
            headers={"x-rapidapi-key": RAPIDAPI_KEY,
                     "x-rapidapi-host": RAPIDAPI_HOST},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ~ RapidAPI не ответил: {str(e)[:120]}")
        return None, None

    contents = (data.get("contents") or [{}])[0]
    videos = contents.get("videos") or []
    audios = contents.get("audios") or []
    if not videos:
        print("  ~ RapidAPI не вернул дорожек")
        return None, None

    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, f"{video_id}.mp4")

    # Вариант 1: дорожка со звуком «как есть» — качаем и всё
    muxed = _pick_stream(videos, want_audio=True)

    # Вариант 2: лучшее видео + оригинальная аудиодорожка, склеиваем ffmpeg
    vid = _pick_stream([v for v in videos
                        if not (v.get("metadata") or {}).get("has_audio")], False)
    original = [a for a in audios
                if (a.get("metadata") or {}).get("is_original")] or audios
    aud = _pick_stream(original, want_audio=True)

    try:
        if vid and aud and shutil.which("ffmpeg"):
            vpath = _fetch(vid[1], os.path.join(tmp, "v.mp4"))
            apath = _fetch(aud[1], os.path.join(tmp, "a.mp4"))
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", vpath, "-i", apath,
                 "-c", "copy", "-movflags", "+faststart", out],
                check=True, timeout=600,
            )
            m = vid[2]
            print(f"  скачано через RapidAPI: {m.get('height')}p со звуком")
            return out, {"width": m.get("width"), "height": m.get("height"),
                         "duration": round((m.get("approx_duration_ms") or 0) / 1000)}

        if muxed:
            _fetch(muxed[1], out)
            m = muxed[2]
            print(f"  скачано через RapidAPI: {m.get('height')}p (готовый файл)")
            return out, {"width": m.get("width"), "height": m.get("height"),
                         "duration": round((m.get("approx_duration_ms") or 0) / 1000)}
    except Exception as e:
        print(f"  ~ RapidAPI: скачать/склеить не вышло: {str(e)[:150]}")

    return None, None


def _fetch(url: str, path: str) -> str:
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return path


def download_video(url: str):
    """Качает ролик через yt-dlp. Возвращает (путь, метаданные) или (None, None)."""
    try:
        import yt_dlp
    except ImportError:
        print("  ~ yt-dlp не установлен — шлём карточкой")
        return None, None

    tmp = tempfile.mkdtemp()
    base = {
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        # bv*+ba — видео вместе со звуком
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "retries": 2,
    }
    ck = cookies_file()
    if ck:
        base["cookiefile"] = ck

    # Пробуем разные клиенты YouTube: у некоторых проверка «не бот» не срабатывает.
    # Последняя попытка — через зеркала Invidious (если плагин установлен).
    attempts = [
        ("tv", {"extractor_args": {"youtube": {"player_client": ["tv"]}}}),
        ("ios", {"extractor_args": {"youtube": {"player_client": ["ios"]}}}),
        ("android", {"extractor_args": {"youtube": {"player_client": ["android"]}}}),
        ("web", {}),
    ]

    try:
        for name, extra in attempts:
            opts = dict(base)
            opts.update(extra)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    path = ydl.prepare_filename(info)
                print(f"  скачано через клиент: {name}")
                return path, {
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "duration": info.get("duration"),
                }
            except Exception as e:
                print(f"  ~ клиент {name} не сработал: {str(e)[:100]}")
        print("  ~ ни один клиент не сработал — шлём карточкой")
        return None, None
    finally:
        if ck:
            try:
                os.remove(ck)
            except OSError:
                pass


def _norm(text: str) -> str:
    """Для сравнения на дубль: без регистра, пунктуации и лишних пробелов."""
    text = re.sub(r"[^\w\s]", "", (text or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


def dedupe_title(title: str, desc: str) -> str:
    """Если описание — это, по сути, повтор заголовка, убираем его."""
    if _norm(desc) == _norm(title):
        return ""
    return desc


def build_video_caption(title: str, desc: str) -> str:
    """Подпись для настоящего видеофайла: просто заголовок и описание, без ссылки и CTA."""
    separators = 2
    room = TG_CAPTION_LIMIT - len(title) - separators

    body = paragraphize((desc or "").strip())
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"

    parts = [f"<b>{html.escape(title)}</b>"]
    if body:
        parts.append(html.escape(body))
    return "\n\n".join(parts)


def send_video(tg_channel: str, path: str, caption: str, meta: dict) -> bool:
    data = {"chat_id": tg_channel, "caption": caption, "parse_mode": "HTML",
            "supports_streaming": True}
    for key in ("width", "height", "duration"):
        if meta and meta.get(key):
            data[key] = int(meta[key])
    with open(path, "rb") as f:
        r = requests.post(f"{TG_API}/sendVideo", data=data,
                          files={"video": f}, timeout=300)
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! Telegram отклонил видео: {r.text[:250]}", file=sys.stderr)
    return bool(ok)


def build_caption(title: str, desc: str, url: str, cta_text: str = None) -> str:
    cta_text = cta_text or CTA_TEXT
    separators = 4
    reserve = len(CTA_EMOJI) * 2 + 8      # эмодзи телега считает за 2 символа
    room = TG_CAPTION_LIMIT - len(title) - len(cta_text) - separators - reserve

    body = (desc or "").strip()
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"
    body = paragraphize(body)

    link = html.escape(url, quote=True)
    parts = [f'<b><a href="{link}">{html.escape(title)}</a></b>']
    if body:
        parts.append(html.escape(body))
    cta = f"{CTA_EMOJI} {cta_text}".strip()
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


def process_channel(handle: str, tg_channel: str, mode: str,
                    skip_shorts: bool, cta_text: str, state: dict) -> None:
    try:
        videos = fetch_feed_with_retry(handle)
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

        short = is_short(v["id"])
        print(f"  проверка на Shorts: {'да' if short else 'нет'}")
        if skip_shorts and short:
            print(f"[{handle}] {v['id']} — Shorts, пропускаем")
            state.setdefault(handle, []).append(v["id"])
            save_state(state)
            continue

        kind = "Shorts" if short else "видео"
        print(f"[{handle}] новое {kind} {v['id']} -> {tg_channel}")
        desc = dedupe_title(v["title"], extract_description(v["description"], mode))

        sent = False
        if short and not SHORTS_AS_VIDEO:
            print("  SHORTS_AS_VIDEO выключен — шлём карточкой")
        if short and SHORTS_AS_VIDEO:
            path, meta = download_via_api(v["id"])
            if not path:
                path, meta = download_video(v["url"])
            if path:
                if os.path.getsize(path) <= TG_UPLOAD_LIMIT:
                    # Настоящий видеофайл: простая подпись, без ссылки и без CTA-кнопки
                    video_caption = build_video_caption(v["title"], desc)
                    sent = send_video(tg_channel, path, video_caption, meta)
                else:
                    print("  ~ файл больше 50 МБ — шлём карточкой")
                try:
                    os.remove(path)
                except OSError:
                    pass

        if not sent:
            # Карточка: заголовок-ссылка + кнопка "Смотреть" — нужны для перехода к видео
            card_caption = build_caption(v["title"], desc, v["url"], cta_text)
            sent = send_post(
                tg_channel, best_thumb(v["id"], v["thumb"], vertical=short), card_caption
            )

        if sent:
            state.setdefault(handle, []).append(v["id"])
            save_state(state)


def main() -> None:
    if not CHANNELS:
        print("YT_CHANNELS не задан — нечего делать")
        return
    state = load_state()
    for handle, tg_channel, mode, skip_shorts, cta in CHANNELS:
        process_channel(handle, tg_channel, mode, skip_shorts, cta, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
