import os
import json
import time
import re
from playwright.sync_api import sync_playwright
import requests

# Ссылка на поиск с вашими фильтрами (Одесса, недвижимость, продажа,
# только от собственников - параметр search[private_business]=private).
# Можно поменять на другую ссылку с сайта - см. README.
OLX_SEARCH_URL = os.environ.get(
    "OLX_SEARCH_URL",
    "https://www.olx.ua/uk/nedvizhimost/odessa/"
    "?currency=USD"
    "&search%5Bfilter_float_price%3Afrom%5D=2000"
    "&search%5Border%5D=created_at%3Adesc"
    "&search%5Bphotos%5D=1"
    "&search%5Bprivate_business%5D=private",
)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    trimmed = list(seen_ids)[-3000:]  # чтобы файл не рос бесконечно
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


def fetch_listings():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="uk-UA")
        page = context.new_page()

        try:
            page.goto(OLX_SEARCH_URL, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="l-card"]', timeout=20000)
        except Exception as e:
            print(f"Не удалось загрузить страницу или найти объявления: {e}")
            browser.close()
            return []

        cards = page.query_selector_all('[data-testid="l-card"]')
        results = []
        for card in cards:
            try:
                card_id = card.get_attribute("id")
                title_el = card.query_selector('[data-testid="card-title-link"]')
                price_el = card.query_selector('[data-testid="ad-price"]')
                loc_el = card.query_selector('[data-testid="location-date"]')

                href = title_el.get_attribute("href") if title_el else None
                if not card_id or not href:
                    continue

                title = title_el.inner_text().strip() if title_el else "Объявление"
                price_raw = price_el.inner_text().strip() if price_el else ""
                # Отделяем "Договірна"/"Договорная" от суммы, если слиплись вместе
                price = re.sub(r"(Договірна|Договорная)$", r" \1", price_raw).strip()
                location = loc_el.inner_text().strip() if loc_el else ""

                url = href if href.startswith("http") else f"https://www.olx.ua{href}"
                # Убираем служебные метки вида ?search_reason=... из ссылки
                url = url.split("?search_reason")[0]

                results.append(
                    {
                        "id": card_id,
                        "title": title,
                        "price": price,
                        "location": location,
                        "url": url,
                    }
                )
            except Exception as e:
                print(f"Пропущена карточка из-за ошибки парсинга: {e}")
                continue

        browser.close()
        return results


def format_message(item):
    lines = [f"🏠 <b>{item['title']}</b>"]
    if item["price"]:
        lines.append(f"Цена: {item['price']}")
    if item["location"]:
        lines.append(item["location"])
    lines.append(item["url"])
    return "\n".join(lines)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=15)
    r.raise_for_status()


def main():
    seen = load_seen()
    listings = fetch_listings()

    if not listings:
        print(
            "Не удалось получить объявления - либо блокировка антиботом, "
            "либо изменилась вёрстка сайта. См. раздел 'Если начались "
            "блокировки' в README."
        )
        return

    print(f"Найдено объявлений на странице: {len(listings)}")

    # Первый запуск: просто запоминаем текущие объявления,
    # чтобы не заспамить чат всей историей сразу
    if not seen:
        save_seen({item["id"] for item in listings})
        print(f"Первый запуск: сохранено {len(listings)} объявлений, уведомления не отправлялись.")
        return

    new_items = [item for item in listings if item["id"] not in seen]

    if not new_items:
        print("Новых объявлений нет.")
        return

    all_seen = set(seen)
    sent = 0
    for item in reversed(new_items):  # от старых к новым
        send_telegram(format_message(item))
        all_seen.add(item["id"])
        sent += 1
        time.sleep(1)  # чтобы не упереться в лимиты Telegram

    save_seen(all_seen)
    print(f"Отправлено новых объявлений: {sent}")


if __name__ == "__main__":
    main()
