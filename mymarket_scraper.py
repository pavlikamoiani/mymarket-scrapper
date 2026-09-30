#!/usr/bin/env python3
"""
Скрапер mymarket.ge -> Telegram
Опрашивает POST https://api.mymarket.ge/api/ka/products по заданным фильтрам,
отсеивает уже виденные product_id (хранятся в seen_ids.json) и отправляет
новые объявления в Telegram-бота.
"""

import json
import time
import os
import requests

# ============== НАСТРОЙКИ ==============

API_URL = "https://api.mymarket.ge/api/ka/products"

# Параметры поиска (можно менять / добавлять свои фильтры)
SEARCH_PARAMS = {
    "Keyword": "iphone 17 pro",
    "PriceTo": "3200",
    "PriceFrom": "2000",
    "OfferPrice": "0",
    "SortID": "1",
    "Limit": 28,
    "Page": 1,
}

# Телеграм
TELEGRAM_BOT_TOKEN = "8799978876:AAHgG99ODeOCzBsiMGWlJktXTjVCxuOUquI"  # <-- вставь сюда полный токен от BotFather
TELEGRAM_CHAT_ID = "1533425276"                # <-- id чата/канала, куда слать сообщения

# Файл, где хранятся уже отправленные ID (дедупликация)
SEEN_IDS_FILE = "seen_ids.json"

# Пауза между запросами при постоянном мониторинге (в секундах)
POLL_INTERVAL = 120  # 2 минуты

# Сколько страниц листать за один прогон (None = только Page, указанный в SEARCH_PARAMS)
MAX_PAGES = None  # например 2, если totalPages в ответе больше 1

# =========================================


def load_seen_ids():
    if os.path.exists(SEEN_IDS_FILE):
        try:
            with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def save_seen_ids(seen_ids):
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_ids), f)


def fetch_products(page=1):
    params = dict(SEARCH_PARAMS)
    params["Page"] = page

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; mymarket-scraper/1.0)",
    }

    resp = requests.post(API_URL, json=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


CONDITION_MAP = {
    0: "New",
    1: "Used",
    2: "Refurbished",
}


def send_to_telegram(text, photo_url=None):
    """Send a styled listing to Telegram. Uses sendPhoto if a photo is
    available (caption = text), otherwise falls back to sendMessage."""
    if photo_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": text,
            "parse_mode": "HTML",
        }
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

    try:
        r = requests.post(url, data=payload, timeout=15)
        if not r.ok:
            print(f"[!] Telegram send error: {r.status_code} - {r.text}")
            # Fallback: if sendPhoto failed (e.g. bad image URL), try plain text
            if photo_url:
                send_to_telegram(text, photo_url=None)
                return
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[!] Telegram send error: {e}")


def format_message(item):
    title = item.get("title", "No title")
    price = item.get("price", "?")
    seo_title = item.get("seo_title", "")
    product_id = item.get("product_id")
    views = item.get("views", 0)
    cond_id = item.get("cond_type_id", 0)
    condition = CONDITION_MAP.get(cond_id, "Unknown")
    seller = item.get("user", "Unknown seller")
    link = f"https://mymarket.ge{seo_title}" if seo_title else ""

    # try to clean price formatting
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


def get_photo_url(item):
    photos = item.get("photos") or []
    if photos:
        return photos[0].get("large", "")
    return None


def run_once():
    seen_ids = load_seen_ids()
    new_count = 0

    page = SEARCH_PARAMS.get("Page", 1)
    while True:
        data = fetch_products(page=page)

        try:
            products = data["data"]["Prs"]
            pagination = data["data"].get("pagination", {})
        except (KeyError, TypeError):
            print("[!] Unexpected response format, check the API.")
            break

        for item in products:
            pid = item.get("product_id")
            if pid is None:
                continue
            if pid in seen_ids:
                continue  # уже видели — пропускаем (дубликат)

            seen_ids.add(pid)
            new_count += 1

            msg = format_message(item)
            photo_url = get_photo_url(item)
            send_to_telegram(msg, photo_url=photo_url)
            print(f"[+] Sent new listing: {pid} - {item.get('title')}")

            time.sleep(1)  # чтобы не спамить телеграм слишком быстро

        # пагинация
        total_pages = pagination.get("totalPages", 1)
        next_page = pagination.get("nextPage")

        if MAX_PAGES is None:
            break
        if next_page and page < min(total_pages, MAX_PAGES):
            page = next_page
        else:
            break

    save_seen_ids(seen_ids)
    print(f"[i] Done. New listings: {new_count}. Total IDs stored: {len(seen_ids)}")


def run_forever():
    while True:
        try:
            run_once()
        except requests.RequestException as e:
            print(f"[!] Request error: {e}")
        except Exception as e:
            print(f"[!] Unexpected error: {e}")

        print(f"[i] Waiting {POLL_INTERVAL} seconds until next check...\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        run_forever()
    else:
        run_once()