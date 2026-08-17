import os
import json
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
import requests

# Ссылка на поиск с вашими фильтрами (Одесса, недвижимость, продажа,
# только от собственников - параметр search[private_business]=private).
# Можно поменять на другую ссылку с сайта - см. README.
# ВАЖНО: используем "or", а не .get(key, default) - если секрет
# OLX_SEARCH_URL не задан в GitHub Actions, переменная окружения всё
# равно приходит как пустая строка, а не отсутствует, и .get() с
# default её не подставит.
DEFAULT_OLX_SEARCH_URL = (
    "https://www.olx.ua/uk/nedvizhimost/odessa/"
    "?currency=USD"
    "&search%5Bfilter_float_price%3Afrom%5D=2000"
    "&search%5Border%5D=created_at%3Adesc"
    "&search%5Bphotos%5D=1"
    "&search%5Bprivate_business%5D=private"
)
OLX_SEARCH_URL = os.environ.get("OLX_SEARCH_URL") or DEFAULT_OLX_SEARCH_URL

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # личный чат - команды/статус
# Куда слать сами уведомления об объявлениях. Если не задано - используется
# TELEGRAM_CHAT_ID (личка), как было раньше.
TELEGRAM_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID") or TELEGRAM_CHAT_ID
# Если группа с темами (topics) и уведомления нужны в конкретную тему -
# укажите её id. Необязательно.
TELEGRAM_NOTIFY_THREAD_ID = os.environ.get("TELEGRAM_NOTIFY_THREAD_ID") or None
SEEN_FILE = "seen.json"
STATE_FILE = "bot_state.json"
TZ = ZoneInfo("Europe/Kyiv")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HELP_TEXT = (
    "🏠 <b>OLX Odesa monitor</b>\n\n"
    "Слежу за новыми объявлениями недвижимости в Одессе от собственников "
    "на OLX и присылаю уведомления, как только появляется что-то новое.\n\n"
    "Проверяю сайт раз в 5 минут, поэтому ответы на команды тоже могут "
    "приходить с задержкой до 5 минут - это не сбой, так работает бесплатное "
    "расписание GitHub Actions.\n\n"
    "<b>Команды:</b>\n"
    "/status - текущий статус мониторинга\n"
    "/help - это сообщение"
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


def load_state():
    default_state = {
        "update_offset": 0,
        "last_check": None,
        "last_new_count": 0,
        "checks_count": 0,
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_state.update(data)
    return default_state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def send_telegram(text, chat_id=None, message_thread_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    r = requests.post(url, data=payload, timeout=15)
    r.raise_for_status()


def status_text(state):
    if state["last_check"]:
        last_check = state["last_check"]
    else:
        last_check = "ещё не было"
    return (
        "✅ <b>Бот работает</b>\n\n"
        f"Последняя проверка: {last_check}\n"
        f"Новых объявлений в последний раз: {state['last_new_count']}\n"
        f"Всего проверок: {state['checks_count']}\n"
        f"Проверяю раз в 5 минут"
    )


def handle_commands(state):
    """Забирает новые входящие сообщения и отвечает на команды.
    Отвечает только в тот же чат, что указан в TELEGRAM_CHAT_ID -
    чтобы посторонние не могли использовать бота, даже если напишут ему."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": state["update_offset"] + 1, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"Не удалось получить обновления Telegram: {e}")
        return

    for update in updates:
        state["update_offset"] = max(state["update_offset"], update["update_id"])
        message = update.get("message")
        if not message:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        # Отвечаем только владельцу бота (тому chat_id, что в секретах)
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        if text.startswith("/start") or text.startswith("/help"):
            send_telegram(HELP_TEXT, chat_id=chat_id)
        elif text.startswith("/status"):
            send_telegram(status_text(state), chat_id=chat_id)


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


def main():
    state = load_state()

    # Сначала отвечаем на команды, которые могли прийти с прошлого запуска
    handle_commands(state)

    seen = load_seen()
    listings = fetch_listings()

    now_str = datetime.now(TZ).strftime("%d.%m %H:%M")
    state["last_check"] = now_str
    state["checks_count"] = state.get("checks_count", 0) + 1

    if not listings:
        print(
            "Не удалось получить объявления - либо блокировка антиботом, "
            "либо изменилась вёрстка сайта. См. раздел 'Если начались "
            "блокировки' в README."
        )
        state["last_new_count"] = 0
        save_state(state)
        return

    print(f"Найдено объявлений на странице: {len(listings)}")

    # Первый запуск: просто запоминаем текущие объявления,
    # чтобы не заспамить чат всей историей сразу
    if not seen:
        save_seen({item["id"] for item in listings})
        state["last_new_count"] = 0
        save_state(state)
        print(f"Первый запуск: сохранено {len(listings)} объявлений, уведомления не отправлялись.")
        return

    new_items = [item for item in listings if item["id"] not in seen]

    if not new_items:
        print("Новых объявлений нет.")
        state["last_new_count"] = 0
        save_state(state)
        return

    # Среди "новых по ID" бывают старые объявления, которые OLX иногда
    # закрепляет наверху выдачи (топ/промо) независимо от даты публикации -
    # они просто раньше не попадали в наш срез. Отправляем уведомление
    # только если в дате стоит "Сьогодні" (сегодня), а остальные молча
    # помечаем как просмотренные, чтобы не проверять их повторно и не слать.
    todays_items = [item for item in new_items if "сьогодні" in item["location"].lower()]
    older_items = [item for item in new_items if item not in todays_items]

    all_seen = set(seen)
    sent = 0
    for item in reversed(todays_items):  # от старых к новым
        send_telegram(
            format_message(item),
            chat_id=TELEGRAM_NOTIFY_CHAT_ID,
            message_thread_id=TELEGRAM_NOTIFY_THREAD_ID,
        )
        all_seen.add(item["id"])
        sent += 1
        time.sleep(1)  # чтобы не упереться в лимиты Telegram

    for item in older_items:
        all_seen.add(item["id"])  # запоминаем, но не уведомляем

    save_seen(all_seen)
    state["last_new_count"] = sent
    save_state(state)
    print(
        f"Отправлено новых объявлений за сегодня: {sent} "
        f"(пропущено старых/промо: {len(older_items)})"
    )


if __name__ == "__main__":
    main()
