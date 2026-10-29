#!/usr/bin/env python3
"""
mymarket.ge -> Telegram scraper with bot command interface.

Commands:
  /search <keyword> [from=N] [to=N]           -- add a new search
  /list                                         -- show all searches with Edit/Delete buttons
  /delete <id>                                  -- remove a search
  /edit <id> [keyword=...] [from=N] [to=N]     -- modify a search
"""

import json
import time
import os
import re
import requests
from datetime import datetime

# ============== НАСТРОЙКИ ==============

API_URL = "https://api.mymarket.ge/api/ka/products"

TELEGRAM_BOT_TOKEN = "8799978876:AAHgG99ODeOCzBsiMGWlJktXTjVCxuOUquI"
TELEGRAM_CHAT_ID = "1533425276"

# Only updates from these chat IDs are processed
ALLOWED_CHAT_IDS = {TELEGRAM_CHAT_ID}

SEARCHES_FILE = "searches.json"
SEEN_IDS_FILE = "seen_ids.json"  # legacy — used only for one-time migration

POLL_INTERVAL = 120  # seconds between scrape runs

# Legacy params — used only when migrating from seen_ids.json
_LEGACY_PARAMS = {
    "Keyword": "iphone 17 pro",
    "PriceFrom": "2000",
    "PriceTo": "8000",
}

CONDITION_MAP = {0: "New", 1: "Used", 2: "Refurbished"}

# =========================================
# Storage
# =========================================

def load_searches() -> dict:
    if os.path.exists(SEARCHES_FILE):
        try:
            with open(SEARCHES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass

    # First run: migrate from legacy seen_ids.json if present
    legacy_seen = []
    if os.path.exists(SEEN_IDS_FILE):
        try:
            with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                legacy_seen = json.load(f)
            print(f"[i] Migrating {len(legacy_seen)} IDs from {SEEN_IDS_FILE}")
        except (json.JSONDecodeError, ValueError):
            pass

    data: dict = {"next_id": 1, "searches": []}

    if legacy_seen or os.path.exists(SEEN_IDS_FILE):
        p = _LEGACY_PARAMS
        entry = {
            "id": 1,
            "keyword": p.get("Keyword", ""),
            "price_from": int(p["PriceFrom"]) if p.get("PriceFrom") else None,
            "price_to": int(p["PriceTo"]) if p.get("PriceTo") else None,
            "seen_ids": legacy_seen,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "active": True,
        }
        data["searches"].append(entry)
        data["next_id"] = 2

    save_searches(data)
    return data


def save_searches(data: dict) -> None:
    tmp = SEARCHES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SEARCHES_FILE)


def get_search_by_id(data: dict, search_id: int) -> dict | None:
    for s in data["searches"]:
        if s["id"] == search_id:
            return s
    return None


# =========================================
# Telegram API wrappers
# =========================================

TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"{TG_BASE}/getUpdates",
            params={
                "offset": offset,
                "timeout": 1,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=5,
        )
        if r.ok:
            return r.json().get("result", [])
    except requests.RequestException as e:
        print(f"[!] getUpdates error: {e}")
    return []


def tg_send_message(chat_id, text: str, reply_markup: dict = None) -> dict | None:
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{TG_BASE}/sendMessage", data=payload, timeout=10)
        if r.ok:
            return r.json().get("result")
        print(f"[!] sendMessage error: {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"[!] sendMessage error: {e}")
    return None


def tg_edit_message(chat_id, message_id: int, text: str, reply_markup: dict = None) -> None:
    payload = {
        "chat_id": str(chat_id),
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{TG_BASE}/editMessageText", data=payload, timeout=10)
        if not r.ok:
            print(f"[!] editMessageText error: {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"[!] editMessageText error: {e}")


def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"{TG_BASE}/answerCallbackQuery",
            data={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except requests.RequestException:
        pass


def tg_send_photo(chat_id, photo_url: str, caption: str) -> None:
    payload = {
        "chat_id": str(chat_id),
        "photo": photo_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(f"{TG_BASE}/sendPhoto", data=payload, timeout=15)
        if not r.ok:
            tg_send_message(chat_id, caption)
    except requests.RequestException as e:
        print(f"[!] sendPhoto error: {e}")
        tg_send_message(chat_id, caption)


# =========================================
# Command parsing helpers
# =========================================

_KV_RE = re.compile(r"^(\w+)=(.+)$")


def parse_kv_args(text: str) -> tuple:
    """
    "iphone 17 pro from=2000 to=8000" -> ("iphone 17 pro", {"from": "2000", "to": "8000"})
    Walks tokens right-to-left; word=value tokens go to kv dict, rest = keyword.
    """
    tokens = text.split()
    kv: dict = {}
    i = len(tokens) - 1
    while i >= 0:
        m = _KV_RE.match(tokens[i])
        if m:
            kv[m.group(1).lower()] = m.group(2)
            i -= 1
        else:
            break
    keyword = " ".join(tokens[: i + 1]).strip()
    return keyword, kv


def _format_price_range(price_from, price_to) -> str:
    if price_from is not None and price_to is not None:
        return f"{price_from:,}–{price_to:,} ₾".replace(",", " ")
    if price_from is not None:
        return f"from {price_from:,} ₾".replace(",", " ")
    if price_to is not None:
        return f"up to {price_to:,} ₾".replace(",", " ")
    return "no price filter"


def _build_list_message(searches: list, header: str = "") -> tuple:
    """Returns (text, reply_markup) for the /list view."""
    lines = []
    if header:
        lines.append(header + "\n")
    lines.append(f"<b>Active searches ({len(searches)}):</b>\n")
    keyboard_rows = []
    for s in searches:
        price_str = _format_price_range(s.get("price_from"), s.get("price_to"))
        lines.append(f"{s['id']}. \"{s['keyword']}\" | {price_str}")
        keyboard_rows.append([
            {"text": f"✏️ Edit {s['id']}", "callback_data": f"edit:{s['id']}"},
            {"text": f"🗑 Delete {s['id']}", "callback_data": f"delete:{s['id']}"},
        ])
    return "\n".join(lines), {"inline_keyboard": keyboard_rows}


# =========================================
# Command handlers
# =========================================

def handle_start_command(chat_id) -> None:
    tg_send_message(
        chat_id,
        "👋 <b>mymarket.ge scraper</b>\n\n"
        "/search &lt;keyword&gt; [from=N] [to=N] — add search\n"
        "/list — show all searches\n"
        "/delete &lt;id&gt; — remove search\n"
        "/edit &lt;id&gt; [keyword=...] [from=N] [to=N] — edit search",
    )


def handle_search_command(chat_id, args: str) -> None:
    if not args.strip():
        tg_send_message(chat_id, "❌ Usage: /search &lt;keyword&gt; [from=N] [to=N]")
        return

    keyword, kv = parse_kv_args(args)
    if not keyword:
        tg_send_message(chat_id, "❌ Keyword required. Example:\n<code>/search iphone 17 pro from=2000</code>")
        return

    price_from = None
    price_to = None
    try:
        if "from" in kv:
            price_from = int(kv["from"])
        if "to" in kv:
            price_to = int(kv["to"])
    except ValueError:
        tg_send_message(chat_id, "❌ Price must be a number. Example: from=2000 to=8000")
        return

    data = load_searches()
    new_id = data["next_id"]
    entry = {
        "id": new_id,
        "keyword": keyword,
        "price_from": price_from,
        "price_to": price_to,
        "seen_ids": [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "active": True,
    }
    data["searches"].append(entry)
    data["next_id"] = new_id + 1
    save_searches(data)

    price_str = _format_price_range(price_from, price_to)
    tg_send_message(
        chat_id,
        f"✅ Search #{new_id} added\n🔍 <b>{keyword}</b>\n💰 {price_str}",
    )
    print(f"[+] New search #{new_id}: {keyword!r} {price_str}")


def handle_list_command(chat_id) -> None:
    data = load_searches()
    active = [s for s in data["searches"] if s.get("active", True)]
    if not active:
        tg_send_message(chat_id, "📭 No active searches.\n\nAdd one:\n<code>/search iphone 17 pro from=2000</code>")
        return
    text, markup = _build_list_message(active)
    tg_send_message(chat_id, text, reply_markup=markup)


def handle_delete_command(chat_id, args: str, message_id: int = None) -> None:
    try:
        search_id = int(args.strip())
    except ValueError:
        tg_send_message(chat_id, "❌ Usage: /delete &lt;id&gt;")
        return

    data = load_searches()
    entry = get_search_by_id(data, search_id)
    if not entry:
        tg_send_message(chat_id, f"❌ Search #{search_id} not found.")
        return

    keyword = entry["keyword"]
    data["searches"] = [s for s in data["searches"] if s["id"] != search_id]
    save_searches(data)
    print(f"[-] Deleted search #{search_id}: {keyword!r}")

    active = [s for s in data["searches"] if s.get("active", True)]
    confirmation = f"✅ Search #{search_id} (<i>{keyword}</i>) deleted."

    if message_id:
        if not active:
            tg_edit_message(chat_id, message_id, confirmation + "\n\n📭 No active searches.")
        else:
            text, markup = _build_list_message(active, header=confirmation)
            tg_edit_message(chat_id, message_id, text, reply_markup=markup)
    else:
        tg_send_message(chat_id, confirmation)


def handle_edit_command(chat_id, args: str) -> None:
    parts = args.strip().split(None, 1)
    if not parts:
        tg_send_message(chat_id, "❌ Usage: /edit &lt;id&gt; [keyword=...] [from=N] [to=N]")
        return
    try:
        search_id = int(parts[0])
    except ValueError:
        tg_send_message(chat_id, "❌ First arg must be search ID (number).")
        return

    rest = parts[1] if len(parts) > 1 else ""

    # keyword= can contain spaces — extract it before from/to kv parsing
    kw_match = re.search(r"keyword=(.+?)(?:\s+(?:from|to)=|$)", rest, re.IGNORECASE)
    keyword_new = kw_match.group(1).strip() if kw_match else None
    rest_no_kw = re.sub(r"keyword=.+?(?=\s+(?:from|to)=|$)", "", rest, flags=re.IGNORECASE).strip()
    _, kv = parse_kv_args(rest_no_kw)

    if not keyword_new and not kv:
        tg_send_message(
            chat_id,
            f"ℹ️ To edit search #{search_id}:\n"
            f"<code>/edit {search_id} keyword=new name from=N to=N</code>\n"
            "(all fields optional)",
        )
        return

    data = load_searches()
    entry = get_search_by_id(data, search_id)
    if not entry:
        tg_send_message(chat_id, f"❌ Search #{search_id} not found.")
        return

    changes = []
    if keyword_new:
        entry["keyword"] = keyword_new
        changes.append(f"keyword → <b>{keyword_new}</b>")
    if "from" in kv:
        try:
            entry["price_from"] = int(kv["from"])
            changes.append(f"from → {kv['from']}")
        except ValueError:
            tg_send_message(chat_id, "❌ from= must be a number.")
            return
    if "to" in kv:
        try:
            entry["price_to"] = int(kv["to"])
            changes.append(f"to → {kv['to']}")
        except ValueError:
            tg_send_message(chat_id, "❌ to= must be a number.")
            return

    save_searches(data)
    tg_send_message(chat_id, f"✅ Search #{search_id} updated:\n" + "\n".join(changes))
    print(f"[~] Edited search #{search_id}: {changes}")


def handle_callback_query(cq: dict) -> None:
    cq_id = cq["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    data_str = cq.get("data", "")

    tg_answer_callback(cq_id)

    if data_str.startswith("delete:"):
        try:
            search_id = int(data_str.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        handle_delete_command(chat_id, str(search_id), message_id=message_id)

    elif data_str.startswith("edit:"):
        try:
            search_id = int(data_str.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        tg_send_message(
            chat_id,
            f"✏️ To edit search #{search_id}:\n"
            f"<code>/edit {search_id} keyword=new name from=N to=N</code>\n"
            "(all fields optional)",
        )


def process_update(update: dict) -> None:
    msg = update.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id not in ALLOWED_CHAT_IDS:
            return
        text = msg.get("text", "")
        if text.startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0].split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            if cmd == "/start":
                handle_start_command(chat_id)
            elif cmd == "/search":
                handle_search_command(chat_id, args)
            elif cmd == "/list":
                handle_list_command(chat_id)
            elif cmd == "/delete":
                handle_delete_command(chat_id, args)
            elif cmd == "/edit":
                handle_edit_command(chat_id, args)

    cq = update.get("callback_query")
    if cq:
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id not in ALLOWED_CHAT_IDS:
            tg_answer_callback(cq["id"])
            return
        handle_callback_query(cq)


# =========================================
# Scraper
# =========================================

def fetch_products_for_search(search: dict, page: int = 1) -> dict:
    body: dict = {
        "Keyword": search["keyword"],
        "SortID": 1,
        "Limit": 28,
        "Page": page,
    }
    if search.get("price_from") is not None:
        body["PriceFrom"] = str(search["price_from"])
    if search.get("price_to") is not None:
        body["PriceTo"] = str(search["price_to"])

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; mymarket-scraper/1.0)",
    }
    resp = requests.post(API_URL, json=body, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def format_message(item: dict) -> str:
    title = item.get("title", "No title")
    price = item.get("price", "?")
    seo_title = item.get("seo_title", "")
    product_id = item.get("product_id")
    views = item.get("views", 0)
    condition = CONDITION_MAP.get(item.get("cond_type_id", 0), "Unknown")
    seller = item.get("user", "Unknown seller")
    link = f"https://mymarket.ge{seo_title}" if seo_title else ""

    try:
        price_fmt = f"{float(price):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        price_fmt = price

    text = (
        f"📱 <b>{title}</b>\n\n"
        f"💰 <b>Price:</b> {price_fmt} ₾\n"
        f"🔧 <b>Condition:</b> {condition}\n"
        f"👤 <b>Seller:</b> {seller}\n"
        f"👁 <b>Views:</b> {views}\n"
        f"🆔 <b>ID:</b> {product_id}\n"
    )
    if link:
        text += f"\n🔗 <a href=\"{link}\">View listing</a>"
    return text


def get_photo_url(item: dict) -> str | None:
    photos = item.get("photos") or []
    if photos:
        return photos[0].get("large", "") or None
    return None


def run_once_for_search(search: dict) -> int:
    seen_ids = set(search.get("seen_ids", []))
    new_count = 0
    page = 1

    while True:
        try:
            data = fetch_products_for_search(search, page=page)
        except requests.RequestException as e:
            print(f"[!] Fetch error for search #{search['id']}: {e}")
            break

        try:
            products = data["data"]["Prs"]
            pagination = data["data"].get("pagination", {})
        except (KeyError, TypeError):
            print(f"[!] Unexpected API response for search #{search['id']}")
            break

        for item in products:
            pid = item.get("product_id")
            if pid is None or pid in seen_ids:
                continue

            seen_ids.add(pid)
            new_count += 1

            msg = format_message(item)
            photo = get_photo_url(item)
            if photo:
                tg_send_photo(TELEGRAM_CHAT_ID, photo, msg)
            else:
                tg_send_message(TELEGRAM_CHAT_ID, msg)
            print(f"[+] Search #{search['id']}: sent {pid} - {item.get('title')}")
            time.sleep(0.5)

        next_page = pagination.get("nextPage")
        total_pages = pagination.get("totalPages", 1)
        if next_page and page < total_pages:
            page = next_page
        else:
            break

    search["seen_ids"] = list(seen_ids)
    return new_count


def run_all_searches() -> None:
    data = load_searches()
    active = [s for s in data["searches"] if s.get("active", True)]
    if not active:
        print("[i] No active searches.")
        return

    total_new = 0
    for search in active:
        n = run_once_for_search(search)
        total_new += n
        if n:
            print(f"[i] Search #{search['id']} ({search['keyword']!r}): {n} new listing(s)")

    save_searches(data)
    print(f"[i] Done. Total new: {total_new}. Active searches: {len(active)}")


# =========================================
# Main loop
# =========================================

def _drain_updates(offset_ref: list) -> None:
    while True:
        updates = tg_get_updates(offset_ref[0])
        if not updates:
            break
        for upd in updates:
            process_update(upd)
            offset_ref[0] = upd["update_id"] + 1


def wait_with_updates(seconds: int, offset_ref: list) -> None:
    for _ in range(seconds):
        updates = tg_get_updates(offset_ref[0])
        for upd in updates:
            process_update(upd)
            offset_ref[0] = upd["update_id"] + 1
        time.sleep(1)


def run_forever() -> None:
    offset_ref = [0]
    print(f"[i] Bot started. Scraping every {POLL_INTERVAL}s. Send /start in Telegram.")
    while True:
        try:
            _drain_updates(offset_ref)
            run_all_searches()
        except Exception as e:
            print(f"[!] Error: {e}")

        print(f"[i] Waiting {POLL_INTERVAL}s...\n")
        wait_with_updates(POLL_INTERVAL, offset_ref)


def run_once() -> None:
    run_all_searches()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        run_forever()
    else:
        run_once()
