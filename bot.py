#!/usr/bin/env python3 """ Репостер TikTok -> Telegram.
Каждому TikTok-аккаунту можно задать свой Telegram-канал и своё поведение с тегами.
Формат TIKTOK_USERS: ник:@канал              — поведение по умолчанию (из KEEP_TAGS) ник:@канал:tags         — теги ОСТАВИТЬ и сделать ссылками ник:@канал:notags       — теги ВЫРЕЗАТЬ
KEEP_TAGS=1 — по умолчанию оставлять теги, KEEP_TAGS=0 — вырезать. """
import html import json import os import pathlib import re import shutil import subprocess import sys import tempfile import time from datetime import datetime, timedelta, timezone from urllib.parse import quote
import requests import yt_dlp
BOT_TOKEN = os.environ["BOT_TOKEN"] DEFAULT_CHANNEL = os.environ.get("CHANNEL_ID", "") KEEP_TAGS_DEFAULT = os.environ.get("KEEP_TAGS", "0") == "1"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
Страховка: видео старше этого числа дней не постим никогда.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))
Сколько последних id помнить по каждому аккаунту (чтобы файл не разрастался).
KEEP_HISTORY = int(os.environ.get("KEEP_HISTORY", "300"))
Основной путь получения видео — через RapidAPI (tikwm/tiktok-scraper7),
он не блокируется по IP раннера так, как прямой yt-dlp. Использует тот же
ключ, что и youtube.py. Если ключа нет или API отказал — падаем на yt-dlp.
TIKTOK_API_KEY = os.environ.get("RAPIDAPI_KEY", "").strip() TIKTOK_API_HOST = os.environ.get("TIKTOK_API_HOST", "tiktok-scraper7.p.rapidapi.com")
Бесплатный план RapidAPI даёт лимит запросов в месяц — экономим его,
проверяя через API каждый аккаунт не чаще, чем раз в N часов.
MIN_API_CHECK_HOURS = float(os.environ.get("MIN_API_CHECK_HOURS", "24")) CHECK_STATE_FILE = pathlib.Path("state_tiktok_checks.json")
def load_check_state() -> dict: if CHECK_STATE_FILE.exists(): raw = CHECK_STATE_FILE.read_text(encoding="utf-8").strip() if raw: try: return json.loads(raw) except json.JSONDecodeError: print("  ~ state_tiktok_checks.json повреждён, начинаем заново", file=sys.stderr) return {}
def save_check_state(cs: dict) -> None: CHECK_STATE_FILE.write_text( json.dumps(cs, ensure_ascii=False, indent=2), encoding="utf-8" )
DEBUG_DESC=1 — печатать сырое описание в лог (для диагностики переносов строк)
DEBUG_DESC = os.environ.get("DEBUG_DESC", "0") == "1"
Разбивать длинные описания на абзацы (короткие не трогаются).
PARAGRAPHS = os.environ.get("PARAGRAPHS", "1") == "1" PARA_MIN = int(os.environ.get("PARA_MIN", "400"))    # с какой длины разбивать PARA_CHUNK = int(os.environ.get("PARA_CHUNK", "320"))  # целевой размер абзаца
Перечень ингредиентов («450 г води») раскладывать в столбик.
COLUMNS = os.environ.get("COLUMNS", "1") == "1" COLUMNS_MIN = int(os.environ.get("COLUMNS_MIN", "3"))  # от скольких позиций считать списком
STATE_FILE = pathlib.Path("state.json") TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" TG_VIDEO_LIMIT = 50 * 1024 * 1024 TG_CAPTION_LIMIT = 1024      # подпись к видео TG_TEXT_LIMIT = 4096         # отдельное сообщение
HASHTAG_RE = re.compile(r"#\w+") HASHTAG_CAP_RE = re.compile(r"#((?=\w*[^\W\d])\w+)") MENTION_RE = re.compile(r"(?<![\w@.])@(A-Za-z0-9_?)") SENT_RE = re.compile(r"(?<=[.!?])\s+")
<число> <единица> — маркер позиции в списке ингредиентов.
Предлоги перед числом («на 4 шт») не считаем началом позиции.
ING_UNITS = r"(?:г|кг|мл|л|шт|ст.?\s?л|ч.?\s?л|зубчик\w*|склянк\w*)" ING_RE = re.compile( r"(?<!\bна)(?<!\bпо)(?<!\bдо)\s+(?=\d+(?:[.,/-]\d+)?\s+" + ING_UNITS + r"\b(?!\s+(?:на|для|у|в)\b))"   # «120 г на кожну» — не позиция списка )
yt-dlp подставляет такой заголовок, когда описания нет — это не текст поста
PLACEHOLDER_RE = re.compile(r"^TikTok video #\d+$", re.IGNORECASE)
def parse_users(raw: str) -> list: """'ник:@канал:tags:часы' в список (ник, канал, теги, интервал_часов).""" result = [] for item in raw.split(","): item = item.strip() if not item: continue
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
def clean_caption(text: str, keep_tags: bool) -> str: """Чистит описание. Теги либо оставляет, либо вырезает.""" text = text or "" if not keep_tags: text = HASHTAG_RE.sub("", text) # TikTok отдаёт описание одной строкой, подставляя неразрывные пробелы # там, где в оригинале были переносы. Приводим их к обычным пробелам. text = text.replace("\xa0", " ").replace("\r", "\n") # Указатели-эмодзи в оригинале начинают новую строку — восстанавливаем. text = re.sub(r"\s*(?=👉)", "\n", text) text = re.sub(r"[ \t]+([,.:;!?])", r"\1", text) text = re.sub(r"[ \t]{2,}", " ", text) text = "\n".join(ln.strip() for ln in text.split("\n")) text = re.sub(r"\n{3,}", "\n\n", text) text = text.strip() if len(text) > TG_TEXT_LIMIT: text = text[: TG_TEXT_LIMIT - 1].rstrip() + "…" return columnize(paragraphize(text))
def _split_long(block: str) -> list: """Режет длинный блок на куски по границам предложений.""" if len(block) <= PARA_CHUNK * 1.4: return [block] chunks, cur = [], "" for sent in SENT_RE.split(block): if cur and len(cur) + len(sent) + 1 > PARA_CHUNK: chunks.append(cur.strip()) cur = sent else: cur = f"{cur} {sent}".strip() if cur.strip(): chunks.append(cur.strip()) return chunks
def paragraphize(text: str) -> str: """Длинное описание разбивает на абзацы, короткое оставляет как есть.""" if not PARAGRAPHS or not text or len(text) <= PARA_MIN: return text out = [] for block in [b.strip() for b in text.split("\n") if b.strip()]: out.extend(_split_long(block)) return "\n\n".join(out)
def columnize(text: str) -> str: """Перечень ингредиентов раскладывает в столбик — если это правда перечень.""" if not COLUMNS or not text: return text out = [] for para in text.split("\n\n"): if len(ING_RE.findall(para)) >= COLUMNS_MIN: para = ING_RE.sub("\n", para) out.append(para) return "\n\n".join(out)
def linkify(escaped: str) -> str: """Делает ссылки из #тегов (на страницу тега) и @упоминаний (на аккаунт)."""
def tag(m):
    word = m.group(1)
    return f'<a href="https://www.tiktok.com/tag/{quote(word)}">#{word}</a>'

def mention(m):
    nick = m.group(1)
    return f'<a href="https://www.tiktok.com/@{quote(nick)}">@{nick}</a>'

return MENTION_RE.sub(mention, HASHTAG_CAP_RE.sub(tag, escaped))
def is_too_old(video_id: str) -> bool: """В id видео TikTok зашита дата публикации (старшие 32 бита).""" try: ts = int(video_id) >> 32 dt = datetime.fromtimestamp(ts, timezone.utc) except (ValueError, OSError, OverflowError): return False if dt.year < 2016: return False          # id непохож на настоящий — не фильтруем return (datetime.now(timezone.utc) - dt) > timedelta(days=MAX_AGE_DAYS)
def load_state() -> dict: if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text(encoding="utf-8")) return {}
def remember(state: dict, user: str, video_id: str) -> None: """Кладёт id наверх списка и подрезает историю.""" lst = state.setdefault(user, []) lst.insert(0, video_id) del lst[KEEP_HISTORY:]
def save_state(state: dict) -> None: """Пишет файл: аккаунты в обратном порядке добавления — новые сверху.""" order = [u for u, _, _, _ in USERS] ordered = {} for user in reversed(order): if user in state: ordered[user] = state[user] for user in state:              # всё, чего нет в настройках, — в конец if user not in ordered: ordered[user] = state[user]
STATE_FILE.write_text(
    json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
)
