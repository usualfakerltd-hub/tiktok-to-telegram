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
import shutil
import subprocess
import sys
import tempfile
import time
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
# Основной путь получения видео — через RapidAPI (tikwm/tiktok-scraper7),
# он не блокируется по IP раннера так, как прямой yt-dlp. Использует тот же
# ключ, что и youtube.py. Если ключа нет или API отказал — падаем на yt-dlp.
TIKTOK_API_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
TIKTOK_API_HOST = os.environ.get("TIKTOK_API_HOST", "tiktok-scraper7.p.rapidapi.com")
# Бесплатный план RapidAPI даёт лимит запросов в месяц — экономим его,
# проверяя через API каждый аккаунт не чаще, чем раз в N часов.
MIN_API_CHECK_HOURS = float(os.environ.get("MIN_API_CHECK_HOURS", "24"))
CHECK_STATE_FILE = pathlib.Path("state_tiktok_checks.json")


def load_check_state() -> dict:
    if CHECK_STATE_FILE.exists():
        return json.loads(CHECK_STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_check_state(cs: dict) -> None:
    CHECK_STATE_FILE.write_text(
        json.dumps(cs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
# DEBUG_DESC=1 — печатать сырое описание в лог (для диагностики переносов строк)
DEBUG_DESC = os.environ.get("DEBUG_DESC", "0") == "1"
# Разбивать длинные описания на абзацы (короткие не трогаются).
PARAGRAPHS = os.environ.get("PARAGRAPHS", "1") == "1"
PARA_MIN = int(os.environ.get("PARA_MIN", "400"))    # с какой длины разбивать
PARA_CHUNK = int(os.environ.get("PARA_CHUNK", "320"))  # целевой размер абзаца
# Перечень ингредиентов («450 г води») раскладывать в столбик.
COLUMNS = os.environ.get("COLUMNS", "1") == "1"
COLUMNS_MIN = int(os.environ.get("COLUMNS_MIN", "3"))  # от скольких позиций считать списком

STATE_FILE = pathlib.Path("state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_VIDEO_LIMIT = 50 * 1024 * 1024
TG_CAPTION_LIMIT = 1024      # подпись к видео
TG_TEXT_LIMIT = 4096         # отдельное сообщение

HASHTAG_RE = re.compile(r"#\w+")
HASHTAG_CAP_RE = re.compile(r"#((?=\w*[^\W\d])\w+)")
MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_](?:[A-Za-z0-9_.]*[A-Za-z0-9_])?)")
SENT_RE = re.compile(r"(?<=[.!?])\s+")
# <число> <единица> — маркер позиции в списке ингредиентов.
# Предлоги перед числом («на 4 шт») не считаем началом позиции.
ING_UNITS = r"(?:г|кг|мл|л|шт|ст\.?\s?л|ч\.?\s?л|зубчик\w*|склянк\w*)"
ING_RE = re.compile(
    r"(?<!\bна)(?<!\bпо)(?<!\bдо)\s+(?=\d+(?:[.,/-]\d+)?\s+"
    + ING_UNITS
    + r"\b(?!\s+(?:на|для|у|в)\b))"   # «120 г на кожну» — не позиция списка
)
# yt-dlp подставляет такой заголовок, когда описания нет — это не текст поста
PLACEHOLDER_RE = re.compile(r"^TikTok video #\d+$", re.IGNORECASE)


def parse_users(raw: str) -> list:
    """'ник:@канал:tags:часы' в список (ник, канал, теги, интервал_часов)."""
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

        interval = MIN_API_CHECK_HOURS
        if len(parts) >= 4 and parts[3].strip():
            try:
                interval = float(parts[3].strip())
            except ValueError:
                pass

        if not chan:
            print(f"[{user}] не задан канал — пропускаем", file=sys.stderr)
            continue
        result.append((user, chan, keep, interval))
    return result


USERS = parse_users(os.environ["TIKTOK_USERS"])


def clean_caption(text: str, keep_tags: bool) -> str:
    """Чистит описание. Теги либо оставляет, либо вырезает."""
    text = text or ""
    if not keep_tags:
        text = HASHTAG_RE.sub("", text)
    # TikTok отдаёт описание одной строкой, подставляя неразрывные пробелы
    # там, где в оригинале были переносы. Приводим их к обычным пробелам.
    text = text.replace("\xa0", " ").replace("\r", "\n")
    # Указатели-эмодзи в оригинале начинают новую строку — восстанавливаем.
    text = re.sub(r"\s*(?=👉)", "\n", text)
    text = re.sub(r"[ \t]+([,.:;!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > TG_TEXT_LIMIT:
        text = text[: TG_TEXT_LIMIT - 1].rstrip() + "…"
    return columnize(paragraphize(text))


def _split_long(block: str) -> list:
    """Режет длинный блок на куски по границам предложений."""
    if len(block) <= PARA_CHUNK * 1.4:
        return [block]
    chunks, cur = [], ""
    for sent in SENT_RE.split(block):
        if cur and len(cur) + len(sent) + 1 > PARA_CHUNK:
            chunks.append(cur.strip())
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def paragraphize(text: str) -> str:
    """Длинное описание разбивает на абзацы, короткое оставляет как есть."""
    if not PARAGRAPHS or not text or len(text) <= PARA_MIN:
        return text
    out = []
    for block in [b.strip() for b in text.split("\n") if b.strip()]:
        out.extend(_split_long(block))
    return "\n\n".join(out)


def columnize(text: str) -> str:
    """Перечень ингредиентов раскладывает в столбик — если это правда перечень."""
    if not COLUMNS or not text:
        return text
    out = []
    for para in text.split("\n\n"):
        if len(ING_RE.findall(para)) >= COLUMNS_MIN:
            para = ING_RE.sub("\n", para)
        out.append(para)
    return "\n\n".join(out)


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


def _list_videos_once(user: str) -> list:
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


def list_videos_via_api(user: str, count: int = 10):
    """Список видео через RapidAPI: даёт готовую ссылку на файл без вотермарки
    и точную дату публикации, поэтому не зависит от блокировки TikTok по IP."""
    if not TIKTOK_API_KEY:
        return None
    try:
        r = requests.get(
            f"https://{TIKTOK_API_HOST}/user/posts",
            params={"unique_id": user, "count": str(count), "cursor": "0"},
            headers={"x-rapidapi-key": TIKTOK_API_KEY, "x-rapidapi-host": TIKTOK_API_HOST},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            print(f"  ~ TikTok API: {data.get('msg')}")
            return None
        out = []
        for v in (data.get("data") or {}).get("videos") or []:
            vid = str(v.get("video_id") or "")
            if not vid:
                continue
            out.append({
                "id": vid,
                "url": f"https://www.tiktok.com/@{user}/video/{vid}",
                "play_url": v.get("play") or "",
                "raw_title": v.get("title") or "",
            })
        return out
    except Exception as e:
        print(f"  ~ TikTok API не ответил: {str(e)[:150]}")
        return None


def list_videos(user: str, attempts: int = 2) -> list:
    api_result = list_videos_via_api(user)
    if api_result is not None:
        return api_result

    print("  ~ используем запасной путь (yt-dlp) — TikTok иногда его блокирует")
    last_err = None
    for i in range(attempts):
        try:
            return _list_videos_once(user)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                print(f"  ~ попытка {i + 1} неудачна, пробую снова через 5с")
                time.sleep(5)
    raise last_err


def _tt_fetch(url: str, path: str, attempts: int = 2) -> str:
    """Качает файл по прямой ссылке. Обрыв соединения — частый разовый сбой."""
    last_err = None
    for i in range(attempts):
        try:
            with requests.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            return path
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                print(f"  ~ скачивание оборвалось ({str(e)[:100]}), пробую снова")
                time.sleep(3)
    raise last_err


def _probe_video(path: str) -> dict:
    """Реальные width/height/duration из файла — надёжнее любых метаданных API."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        dur = fmt.get("duration")
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration": round(float(dur)) if dur else None,
        }
    except Exception:
        return {}


def download(v: dict, keep_tags: bool):
    """Качает видео. Есть прямая ссылка из API — качаем без yt-dlp вообще."""
    play_url = v.get("play_url")
    if play_url:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, f"{v['id']}.mp4")
        try:
            _tt_fetch(play_url, path)
            meta = _probe_video(path)
            raw = v.get("raw_title", "")
            if DEBUG_DESC:
                print("  --- сырое описание (repr, первые 600 символов) ---")
                print("  " + repr(raw[:600]))
                print("  --- конец ---")
            return path, clean_caption(raw, keep_tags), meta
        except Exception as e:
            print(f"  ~ прямая ссылка не сработала ({str(e)[:120]}), пробую yt-dlp")

    return download_via_ytdlp(v["url"], keep_tags)


def download_via_ytdlp(url: str, keep_tags: bool):
    tmp = tempfile.mkdtemp()
    opts = {
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        # bv*+ba — видео вместе со звуком; просто "mp4" может дать дорожку без аудио
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    meta = {
        "width": info.get("width"),
        "height": info.get("height"),
        "duration": info.get("duration"),
    }
    raw = info.get("description") or ""
    if DEBUG_DESC:
        print("  --- сырое описание (repr, первые 600 символов) ---")
        print("  " + repr(raw[:600]))
        print(f"  переносов строк в описании: {raw.count(chr(10))}")
        print("  --- конец ---")
    if not raw.strip():
        title = (info.get("title") or "").strip()
        raw = "" if PLACEHOLDER_RE.match(title) else title
    return path, clean_caption(raw, keep_tags), meta


def _format(text: str, keep_tags: bool) -> dict:
    """Готовит поля текста: со ссылками на теги или обычным текстом."""
    if keep_tags:
        return {"text": linkify(html.escape(text)), "parse_mode": "HTML"}
    return {"text": text}


def send_text(channel: str, text: str, keep_tags: bool) -> bool:
    body = _format(text, keep_tags)
    data = {"chat_id": channel, "text": body["text"]}
    if "parse_mode" in body:
        data["parse_mode"] = body["parse_mode"]
    r = requests.post(f"{TG_API}/sendMessage", data=data, timeout=60)
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! текст не ушёл: {r.text[:250]}", file=sys.stderr)
    return bool(ok)


def send_video(path: str, caption: str, channel: str, keep_tags: bool,
               meta: dict = None) -> bool:
    """Короткое описание идёт подписью, длинное — отдельным сообщением следом."""
    split = len(caption) > TG_CAPTION_LIMIT

    data = {"chat_id": channel, "supports_streaming": True}
    # Без размеров Telegram подставляет свои и картинка выглядит сплющенной.
    for key in ("width", "height", "duration"):
        if meta and meta.get(key):
            data[key] = int(meta[key])
    if not split and caption:
        body = _format(caption, keep_tags)
        data["caption"] = body["text"]
        if "parse_mode" in body:
            data["parse_mode"] = body["parse_mode"]

    with open(path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendVideo", data=data, files={"video": f}, timeout=180
        )
    ok = r.ok and r.json().get("ok")
    if not ok:
        print(f"  ! Telegram отклонил: {r.text[:300]}", file=sys.stderr)
        return False

    if split:
        print(f"  описание длинное ({len(caption)}) — шлём отдельным сообщением")
        send_text(channel, caption, keep_tags)
    return True


def process_user(user: str, channel: str, keep_tags: bool, interval_hours: float,
                  state: dict, check_state: dict) -> bool:
    """Возвращает False, если не удалось даже получить список видео (для алертов)."""
    if TIKTOK_API_KEY:
        last = check_state.get(user)
        if last is not None:
            elapsed_h = (time.time() - last) / 3600
            if elapsed_h < interval_hours:
                print(f"[{user}] проверка отложена — экономим лимит API "
                      f"(ещё {interval_hours - elapsed_h:.1f} ч)")
                return True

    posted = set(state.get(user, []))
    try:
        videos = list_videos(user)
    except Exception as e:
        if TIKTOK_API_KEY:
            check_state[user] = time.time()
        print(f"[{user}] не удалось получить список: {e}", file=sys.stderr)
        return False

    if TIKTOK_API_KEY:
        check_state[user] = time.time()

    if not videos:
        print(f"[{user}] список пуст (возможно, TikTok ограничил IP)")
        return False

    if user not in state:
        state[user] = [v["id"] for v in videos][:KEEP_HISTORY]
        print(f"[{user}] первый запуск — засеяли {len(videos)} видео, посты не шлём")
        return True

    unseen = list(reversed([v for v in videos if v["id"] not in posted]))

    # Старьё отбраковываем пачкой сразу — оно не должно съедать лимит на посты.
    stale = [v for v in unseen if is_too_old(v["id"])]
    if stale:
        # В файл не пишем: возраст считается из самого id, запросов не требует.
        print(f"[{user}] старее {MAX_AGE_DAYS} дн. — пропущено: {len(stale)}")

    new = [v for v in unseen if not is_too_old(v["id"])][:MAX_PER_RUN]
    if not new:
        print(f"[{user}] новых видео нет")
        return True

    for v in new:
        print(f"[{user}] новое видео {v['id']} -> {channel}")
        try:
            path, caption, meta = download(v, keep_tags)
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

        if send_video(path, caption, channel, keep_tags, meta):
            remember(state, user, v["id"])
            save_state(state)
        os.remove(path)
    return True


def main() -> None:
    state = load_state()
    check_state = load_check_state()
    ok, total = 0, 0
    for user, channel, keep_tags, interval_hours in USERS:
        total += 1
        if process_user(user, channel, keep_tags, interval_hours, state, check_state):
            ok += 1
    save_state(state)
    save_check_state(check_state)
    print("готово")
    if total and ok == 0:
        # Все источники разом отдали ошибку — это не «постов нет», а системный сбой
        # (блокировка IP, битый секрет и т.п.). Роняем прогон, чтобы сработал алерт.
        print(f"ВСЕ {total} источников недоступны — считаем это сбоем", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
