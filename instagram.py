#!/usr/bin/env python3
"""
Репостер Instagram -> Telegram (через Apify).

Тянет новые посты публичного профиля: фото, Reels и карусели.
Карусель уходит альбомом, подпись — с хэштегами-ссылками.

Формат IG_ACCOUNTS:  ник:@телеграм_канал, ник2:@канал2
Состояние — в state_instagram.json.
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

BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))
FETCH_LIMIT = int(os.environ.get("IG_FETCH_LIMIT", "3"))   # сколько постов брать за заход
KEEP_HISTORY = int(os.environ.get("KEEP_HISTORY", "300"))

STATE_FILE = pathlib.Path("state_instagram.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
APIFY_URL = "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items"

TG_CAPTION_LIMIT = 1024
TG_TEXT_LIMIT = 4096
TG_UPLOAD_LIMIT = 50 * 1024 * 1024

HASHTAG_RE = re.compile(r"#((?=\w*[^\W\d])\w+)")
MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_](?:[A-Za-z0-9_.]*[A-Za-z0-9_])?)")


def parse_accounts(raw: str) -> list:
    """'ник:@канал' или 'ник:@канал:лимит' — сколько постов брать за заход."""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        parts = item.split(":")
        user = parts[0].strip().lstrip("@")
        chan = parts[1].strip() if len(parts) > 1 else ""
        limit = FETCH_LIMIT
        if len(parts) > 2 and parts[2].strip().isdigit():
            limit = int(parts[2].strip())
        if user and chan:
            result.append((user, chan, limit))
    return result


def load_groups() -> list:
    """Пары (список аккаунтов, токен). Второй набор — необязательный."""
    groups = []
    for acc_var, tok_var in (
        ("IG_ACCOUNTS", "APIFY_TOKEN"),
        ("IG_ACCOUNTS_2", "APIFY_TOKEN_2"),
    ):
        raw = os.environ.get(acc_var, "").strip()
        token = os.environ.get(tok_var, "").strip()
        if not raw or not token:
            continue
        accounts = parse_accounts(raw)
        if accounts:
            groups.append((accounts, token))
    return groups


GROUPS = load_groups()
ACCOUNTS = [a for accs, _ in GROUPS for a in accs]


def clean_caption(text: str) -> str:
    """Instagram отдаёт переносы строк как есть — их сохраняем."""
    text = (text or "").replace("\xa0", " ")
    # Instagram иногда отдаёт апострофы и кавычки как HTML-сущности
    # (например, "&#x27;") — раскодируем их в обычные символы.
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def linkify(escaped: str) -> str:
    """Хэштеги ведут на страницу тега, упоминания — на профиль."""

    def tag(m):
        w = m.group(1)
        return f'<a href="https://www.instagram.com/explore/tags/{quote(w)}/">#{w}</a>'

    def mention(m):
        n = m.group(1)
        return f'<a href="https://www.instagram.com/{quote(n)}/">@{n}</a>'

    return MENTION_RE.sub(mention, HASHTAG_RE.sub(tag, escaped))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def remember(state: dict, user: str, post_id: str) -> None:
    lst = state.setdefault(user, [])
    lst.insert(0, post_id)
    del lst[KEEP_HISTORY:]


def save_state(state: dict) -> None:
    order = [u for u, _, _ in ACCOUNTS]
    ordered = {}
    for user in reversed(order):
        if user in state:
            ordered[user] = state[user]
    for user in state:
        if user not in ordered:
            ordered[user] = state[user]
    STATE_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_posts(user: str, token: str, limit: int) -> list:
    """Запрашивает последние посты профиля через Apify."""
    payload = {
        "directUrls": [f"https://www.instagram.com/{user}/"],
        "resultsType": "posts",
        "resultsLimit": limit,
        "addParentData": False,
    }
    r = requests.post(APIFY_URL, params={"token": token}, json=payload, timeout=300)
    if not r.ok:
        print(f"  ~ Apify ответил {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    items = r.json()

    if items:
        print(f"  поля первого элемента: {sorted(items[0].keys())}")

    posts = []
    for it in items:
        pid = it.get("id") or it.get("shortCode")
        if not pid:
            continue
        posts.append(
            {
                "id": str(pid),
                "type": it.get("type") or "",
                "caption": it.get("caption") or "",
                "timestamp": it.get("timestamp") or "",
                "url": it.get("url") or "",
                "displayUrl": it.get("displayUrl") or "",
                "videoUrl": it.get("videoUrl") or "",
                "children": it.get("childPosts") or [],
                "images": it.get("images") or [],
            }
        )

    # Порядок из ответа Apify ненадёжен: закреплённые посты вылезают вперёд.
    # Сортируем сами — новые первыми.
    posts.sort(key=lambda p: p["timestamp"] or "", reverse=True)

    print(f"  получено постов: {len(posts)} (после сортировки, сверху новее)")
    for p in posts:
        print(f"    {p['id']}  {p['timestamp'] or 'ДАТЫ НЕТ':<28} {p['type']:<8} {p['url']}")

    return posts


def is_too_old(timestamp: str) -> bool:
    if not timestamp:
        return False
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt) > timedelta(days=MAX_AGE_DAYS)


def media_list(post: dict) -> list:
    """Собирает список медиа поста: [(тип, url), ...]. Карусель — все элементы."""
    out = []
    if post["children"]:
        for ch in post["children"][:10]:      # Telegram: максимум 10 в альбоме
            if ch.get("videoUrl"):
                out.append(("video", ch["videoUrl"]))
            elif ch.get("displayUrl"):
                out.append(("photo", ch["displayUrl"]))
    elif post["videoUrl"]:
        out.append(("video", post["videoUrl"]))
    elif post["images"]:
        for u in post["images"][:10]:
            out.append(("photo", u))
    elif post["displayUrl"]:
        out.append(("photo", post["displayUrl"]))
    return out


def download(url: str, suffix: str) -> str:
    """Качает медиа во временный файл."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    return path


def chunk_text(text: str, limit: int = TG_TEXT_LIMIT - 100) -> list:
    """Режет длинный текст на части по абзацам, затем по предложениям."""
    if len(text) <= limit:
        return [text]

    parts, cur = [], ""
    for para in text.split("\n\n"):
        piece = para if not cur else f"{cur}\n\n{para}"
        if len(piece) <= limit:
            cur = piece
            continue
        if cur:
            parts.append(cur)
            cur = ""
        # абзац сам по себе длиннее лимита — режем по предложениям
        if len(para) > limit:
            for sent in re.split(r"(?<=[.!?])\s+", para):
                cand = sent if not cur else f"{cur} {sent}"
                if len(cand) <= limit:
                    cur = cand
                else:
                    if cur:
                        parts.append(cur)
                    cur = sent[:limit]
        else:
            cur = para
    if cur:
        parts.append(cur)
    return parts


def send_text(channel: str, text: str) -> bool:
    parts = chunk_text(text)
    if len(parts) > 1:
        print(f"  текст длинный ({len(text)}) — шлём {len(parts)} частями")

    all_ok = True
    for part in parts:
        r = requests.post(
            f"{TG_API}/sendMessage",
            data={
                "chat_id": channel,
                "text": linkify(html.escape(part, quote=False)),
                "parse_mode": "HTML",
            },
            timeout=60,
        )
        if not (r.ok and r.json().get("ok")):
            print(f"  ! часть текста не ушла: {r.text[:250]}", file=sys.stderr)
            all_ok = False
    return all_ok


def send_post(channel: str, post: dict) -> bool:
    media = media_list(post)
    if not media:
        print("  ! в посте нет медиа — пропускаем", file=sys.stderr)
        return False

    caption = clean_caption(post["caption"])
    split = len(caption) > TG_CAPTION_LIMIT

    paths = []
    try:
        for kind, url in media:
            suffix = ".mp4" if kind == "video" else ".jpg"
            try:
                p = download(url, suffix)
            except Exception as e:
                print(f"  ! медиа не скачалось: {e}", file=sys.stderr)
                continue
            if os.path.getsize(p) > TG_UPLOAD_LIMIT:
                print("  ! файл больше 50 МБ — пропускаем", file=sys.stderr)
                os.remove(p)
                continue
            paths.append((kind, p))

        if not paths:
            return False

        # одиночное медиа
        if len(paths) == 1:
            kind, path = paths[0]
            method = "sendVideo" if kind == "video" else "sendPhoto"
            field = "video" if kind == "video" else "photo"
            data = {"chat_id": channel}
            if not split and caption:
                data["caption"] = linkify(html.escape(caption, quote=False))
                data["parse_mode"] = "HTML"
            with open(path, "rb") as f:
                r = requests.post(
                    f"{TG_API}/{method}", data=data, files={field: f}, timeout=300
                )
        # альбом
        else:
            group, files = [], {}
            for i, (kind, path) in enumerate(paths):
                name = f"m{i}"
                entry = {"type": kind, "media": f"attach://{name}"}
                if i == 0 and not split and caption:
                    entry["caption"] = linkify(html.escape(caption, quote=False))
                    entry["parse_mode"] = "HTML"
                group.append(entry)
                files[name] = open(path, "rb")
            try:
                r = requests.post(
                    f"{TG_API}/sendMediaGroup",
                    data={"chat_id": channel, "media": json.dumps(group)},
                    files=files,
                    timeout=300,
                )
            finally:
                for f in files.values():
                    f.close()

        ok = r.ok and r.json().get("ok")
        if not ok:
            print(f"  ! Telegram отклонил: {r.text[:300]}", file=sys.stderr)
            return False

        if split:
            print(f"  подпись длинная ({len(caption)}) — шлём отдельно")
            send_text(channel, caption)
        return True

    finally:
        for _, p in paths:
            try:
                os.remove(p)
            except OSError:
                pass


def process(user: str, channel: str, token: str, limit: int, state: dict) -> None:
    try:
        posts = fetch_posts(user, token, limit)
    except Exception as e:
        print(f"[{user}] Apify не ответил: {e}", file=sys.stderr)
        return

    if not posts:
        print(f"[{user}] постов не вернулось")
        return

    if user not in state:
        state[user] = [p["id"] for p in posts][:KEEP_HISTORY]
        print(f"[{user}] первый запуск — засеяли {len(posts)}, посты не шлём")
        return

    posted = set(state.get(user, []))
    unseen = list(reversed([p for p in posts if p["id"] not in posted]))

    stale = [p for p in unseen if is_too_old(p["timestamp"])]
    if stale:
        print(f"[{user}] старее {MAX_AGE_DAYS} дн. — пропущено: {len(stale)}")

    new = [p for p in unseen if not is_too_old(p["timestamp"])][:MAX_PER_RUN]
    if not new:
        print(f"[{user}] новых постов нет")
        return

    for p in new:
        print(f"[{user}] новый пост {p['id']} ({p['type']}) -> {channel}")
        if send_post(channel, p):
            remember(state, user, p["id"])
            save_state(state)


def main() -> None:
    if not GROUPS:
        print("IG_ACCOUNTS / APIFY_TOKEN не заданы — нечего делать")
        return
    state = load_state()
    for i, (accounts, token) in enumerate(GROUPS, 1):
        print(f"=== набор {i}: аккаунтов {len(accounts)} ===")
        for user, channel, limit in accounts:
            process(user, channel, token, limit, state)
    save_state(state)
    print("готово")


if __name__ == "__main__":
    main()
