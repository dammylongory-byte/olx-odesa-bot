import os
import json
import re
import time
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
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # личный чат - команды/статус/алерты
# Куда слать сами уведомления об объявлениях. Если не задано - используется
# TELEGRAM_CHAT_ID (личка), как было раньше.
TELEGRAM_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID") or TELEGRAM_CHAT_ID
# Если группа с темами (topics) и уведомления нужны в конкретную тему -
# укажите её id. Необязательно.
TELEGRAM_NOTIFY_THREAD_ID = os.environ.get("TELEGRAM_NOTIFY_THREAD_ID") or None
SEEN_FILE = "seen.json"
STATE_FILE = "bot_state.json"
TZ = ZoneInfo("Europe/Kyiv")

# Ночной режим: не проверяем сайт в это время (по киевскому времени).
# Расписание в GitHub Actions тоже не запускает джобу в это время - здесь
# это подстраховка на случай ручного запуска или сдвига по границе часа.
NIGHT_START_HOUR = 2
NIGHT_END_HOUR = 6

# После скольки проверок подряд без объявлений слать тревогу в личку.
FAILURE_ALERT_THRESHOLD = 3
# Через сколько дополнительных неудачных проверок повторять тревогу,
# если проблема не решилась (12 проверок * 5 минут = ~1 час).
FAILURE_ALERT_REPEAT_EVERY = 12

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HELP_TEXT = (
    "🏠 <b>OLX Odesa monitor</b>\n\n"
    "Слежу за новыми объявлениями недвижимости в Одессе от собственников "
    "на OLX и присылаю уведомления, как только появляется что-то новое.\n\n"
    "Проверяю сайт раз в 5 минут (кроме ночи с 2:00 до 6:00), поэтому "
    "ответы на команды тоже могут приходить с задержкой - это не сбой, "
    "так работает бесплатное расписание GitHub Actions.\n\n"
    "<b>Команды:</b>\n"
    "/status - текущий статус мониторинга\n"
    "/stats - статистика за сегодня и за 7 дней\n"
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
        "consecutive_failures": 0,
        "daily_stats": {},  # {"17.08.2026": {"count": N, "price_sum": X, "price_count": M}}
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_state.update(data)
    return default_state


def save_state(state):
    # оставляем статистику только за последние 8 дней, чтобы файл не рос
    if len(state.get("daily_stats", {})) > 8:
        sorted_dates = sorted(
            state["daily_stats"].keys(),
            key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
        )
        for old_date in sorted_dates[:-8]:
            del state["daily_stats"][old_date]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def is_night_time():
    hour = datetime.now(TZ).hour
    return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR


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


def parse_price_usd(price_str):
    """Вытаскивает число из строки вида '32 000 $ Договірна' -> 32000.
    Если цена договорная без числа - возвращает None."""
    match = re.search(r"([\d\s]+)\s*\$", price_str or "")
    if not match:
        return None
    digits = match.group(1).replace(" ", "").replace("\xa0", "")
    return int(digits) if digits.isdigit() else None


def status_text(state):
    last_check = state["last_check"] or "ещё не было"
    lines = [
        "✅ <b>Бот работает</b>\n",
        f"Последняя проверка: {last_check}",
        f"Новых объявлений в последний раз: {state['last_new_count']}",
        f"Всего проверок: {state['checks_count']}",
    ]
    if state["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        lines.append(
            f"⚠️ Проверок подряд без объявлений: {state['consecutive_failures']}"
        )
    lines.append("Проверяю раз в 5 минут (кроме ночи 2:00-6:00)")
    return "\n".join(lines)


def stats_text(state):
    today_key = datetime.now(TZ).strftime("%d.%m.%Y")
    daily = state.get("daily_stats", {})
    today = daily.get(today_key, {"count": 0, "price_sum": 0, "price_count": 0})

    week_count = sum(d["count"] for d in daily.values())
    week_price_sum = sum(d["price_sum"] for d in daily.values())
    week_price_count = sum(d["price_count"] for d in daily.values())

    lines = ["📊 <b>Статистика</b>\n", "<b>Сегодня:</b>", f"Новых объявлений: {today['count']}"]
    if today["price_count"] > 0:
        avg_today = today["price_sum"] // today["price_count"]
        lines.append(f"Средняя цена: ${avg_today:,}".replace(",", " "))

    lines.append("")
    lines.append(f"<b>За последние {len(daily)} дн.:</b>")
    lines.append(f"Новых объявлений: {week_count}")
    if week_price_count > 0:
        avg_week = week_price_sum // week_price_count
        lines.append(f"Средняя цена: ${avg_week:,}".replace(",", " "))

    return "\n".join(lines)


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
        elif text.startswith("/stats"):
            send_telegram(stats_text(state), chat_id=chat_id)


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


def record_stats(state, sent_items):
    """Обновляет дневную статистику отправленными сегодня объявлениями."""
    today_key = datetime.now(TZ).strftime("%d.%m.%Y")
    daily = state.setdefault("daily_stats", {})
    today = daily.setdefault(today_key, {"count": 0, "price_sum": 0, "price_count": 0})

    for item in sent_items:
        today["count"] += 1
        price = parse_price_usd(item["price"])
        if price is not None:
            today["price_sum"] += price
            today["price_count"] += 1


def handle_fetch_failure(state):
    """Считает подряд идущие неудачные проверки и шлёт тревогу в личку."""
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    failures = state["consecutive_failures"]

    should_alert = failures == FAILURE_ALERT_THRESHOLD or (
        failures > FAILURE_ALERT_THRESHOLD
        and (failures - FAILURE_ALERT_THRESHOLD) % FAILURE_ALERT_REPEAT_EVERY == 0
    )
    if should_alert:
        send_telegram(
            "⚠️ <b>Проблема с мониторингом OLX</b>\n\n"
            f"Уже {failures} проверок подряд не удалось получить объявления - "
            "похоже на блокировку антиботом или OLX поменял вёрстку сайта.\n\n"
            "Загляните в GitHub → Actions → последний запуск → лог 'Run monitor', "
            "там будет подробность. Раздел README 'Если начались блокировки' "
            "подскажет, что делать дальше."
        )


def main():
    state = load_state()

    # Сначала отвечаем на команды, которые могли прийти с прошлого запуска -
    # это дёшево (без браузера), поэтому делаем даже ночью.
    handle_commands(state)

    if is_night_time():
        print("Ночной режим (2:00-6:00) - пропускаем проверку сайта.")
        save_state(state)
        return

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
        handle_fetch_failure(state)
        save_state(state)
        return

    state["consecutive_failures"] = 0  # сайт снова отвечает нормально
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
    sent_items = []
    for item in reversed(todays_items):  # от старых к новым
        send_telegram(
            format_message(item),
            chat_id=TELEGRAM_NOTIFY_CHAT_ID,
            message_thread_id=TELEGRAM_NOTIFY_THREAD_ID,
        )
        all_seen.add(item["id"])
        sent_items.append(item)
        time.sleep(1)  # чтобы не упереться в лимиты Telegram

    for item in older_items:
        all_seen.add(item["id"])  # запоминаем, но не уведомляем

    record_stats(state, sent_items)
    save_seen(all_seen)
    state["last_new_count"] = len(sent_items)
    save_state(state)
    print(
        f"Отправлено новых объявлений за сегодня: {len(sent_items)} "
        f"(пропущено старых/промо: {len(older_items)})"
    )


if __name__ == "__main__":
    main()
