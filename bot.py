"""Telegram-бот «Дайджест»: pyTelegramBotAPI + sqlite3 + feedparser + Groq.

Запускается локально: python bot.py
Планировщик — обычный фоновый поток, который раз в 30 секунд проверяет,
кому пора отправить дайджест (APScheduler не нужен).
"""
import html
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import telebot
from dotenv import load_dotenv
from telebot.types import LinkPreviewOptions, Message

load_dotenv()  # переменные должны быть загружены до импорта llm (ему нужен GROQ_API_KEY)

import db  # noqa: E402
import llm  # noqa: E402
import rss  # noqa: E402

logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(os.environ["BOT_TOKEN"], parse_mode="HTML")

CHECK_INTERVAL = 30       # как часто планировщик проверяет расписание, сек
CATCHUP_MINUTES = 180     # сколько минут после назначенного времени ещё можно отправить дайджест
                          # (если компьютер был выключен или спал)

HELP = (
    "<b>Команды</b>\n"
    "/add <i>название</i> — добавить новостной сайт (например, /add BBC)\n"
    "/sources — список источников\n"
    "/remove <i>номер</i> — удалить источник\n"
    "/time <i>ЧЧ:ММ</i> — время ежедневного дайджеста\n"
    "/tz <i>Europe/Moscow</i> — ваш часовой пояс\n"
    "/interests — ваши темы для фильтрации\n"
    "/addinterest <i>темы через запятую</i> — добавить темы\n"
    "/delinterest <i>номера или названия</i> — убрать темы\n"
    "/clearinterests — удалить все темы\n"
    "/digest — получить дайджест прямо сейчас"
)


def get_args(m: Message) -> str:
    """Текст после команды: '/add BBC News' -> 'BBC News'."""
    parts = (m.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# ---------------------------------------------------------------- дайджест
def send_digest(user_id: int) -> None:
    user = db.get_user(user_id)
    sources = db.get_sources(user_id)
    if not sources:
        bot.send_message(user_id, "Источников пока нет. Добавьте через /add.")
        return

    items = rss.fetch_new_items(user_id, sources)
    if not items:
        bot.send_message(user_id, "Новых новостей нет.")
        return

    interests = ", ".join(t["topic"] for t in db.get_topics(user_id))
    try:
        picks = llm.make_digest(items, interests)
    except Exception:
        logging.exception("Ошибка LLM")
        bot.send_message(user_id, "Не удалось подготовить дайджест, попробую позже.")
        return

    if not picks:
        bot.send_message(user_id, "Подходящих новостей не нашлось.")
    else:
        lines = ["<b>Ваш дайджест</b>\n"]
        for p in picks:
            it = p["item"]
            lines.append(
                f"• <a href=\"{html.escape(it['link'], quote=True)}\">"
                f"{html.escape(it['title'])}</a>\n  {html.escape(p['summary'])}"
            )
        text = "\n".join(lines)[:4000]  # лимит Telegram — 4096 символов
        bot.send_message(
            user_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

    # помечаем все просмотренные записи, чтобы не возвращаться к ним завтра
    db.mark_sent(user_id, [it["link"] for it in items])


# ------------------------------------------------------------- планировщик
def is_due(user, now_utc: datetime) -> bool:
    """Пора ли слать дайджест: время наступило, но не позже CATCHUP_MINUTES, и сегодня ещё не слали."""
    local = now_utc.astimezone(ZoneInfo(user["tz"]))
    if user["last_sent_date"] == local.date().isoformat():
        return False
    h, mi = map(int, user["send_time"].split(":"))
    diff = (local.hour * 60 + local.minute) - (h * 60 + mi)
    return 0 <= diff <= CATCHUP_MINUTES


def scheduler_loop() -> None:
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            for user in db.all_users():
                if is_due(user, now_utc):
                    today = now_utc.astimezone(ZoneInfo(user["tz"])).date().isoformat()
                    db.update_user(user["user_id"], last_sent_date=today)  # сначала отметка, чтобы не слать дважды
                    threading.Thread(
                        target=send_digest, args=(user["user_id"],), daemon=True
                    ).start()
        except Exception:
            logging.exception("Ошибка планировщика")
        time.sleep(CHECK_INTERVAL)


# ------------------------------------------------------------------ команды
@bot.message_handler(commands=["start", "help"])
def cmd_start(m: Message):
    db.ensure_user(m.from_user.id)
    bot.reply_to(m, "Привет! Я собираю персональный дайджест новостей.\n\n" + HELP)


@bot.message_handler(commands=["add"])
def cmd_add(m: Message):
    name = get_args(m)
    if not name:
        return bot.reply_to(m, "Укажите название сайта: /add BBC")
    db.ensure_user(m.from_user.id)
    bot.reply_to(m, "Ищу RSS-ленту…")
    try:
        found = llm.find_feed(name)
    except Exception:
        logging.exception("Ошибка поиска ленты")
        return bot.send_message(m.chat.id, "Ошибка при поиске. Попробуйте позже.")
    if not found:
        return bot.send_message(m.chat.id, "Не удалось найти рабочую RSS-ленту для этого сайта.")
    title, url = found
    added = db.add_source(m.from_user.id, title, url)
    bot.send_message(
        m.chat.id, f"Добавлено: {html.escape(title)}" if added else "Этот источник уже в списке."
    )


@bot.message_handler(commands=["sources"])
def cmd_sources(m: Message):
    sources = db.get_sources(m.from_user.id)
    if not sources:
        return bot.reply_to(m, "Источников нет. Добавьте через /add.")
    bot.reply_to(m, "\n".join(f"{s['id']}. {html.escape(s['title'])}" for s in sources))


@bot.message_handler(commands=["remove"])
def cmd_remove(m: Message):
    arg = get_args(m)
    if not arg.isdigit():
        return bot.reply_to(m, "Укажите номер из /sources: /remove 3")
    ok = db.remove_source(m.from_user.id, int(arg))
    bot.reply_to(m, "Удалено." if ok else "Источник не найден.")


@bot.message_handler(commands=["time"])
def cmd_time(m: Message):
    try:
        h, mi = map(int, get_args(m).split(":"))
        assert 0 <= h < 24 and 0 <= mi < 60
    except Exception:
        return bot.reply_to(m, "Формат: /time 08:30")
    db.ensure_user(m.from_user.id)
    # сбрасываем отметку, чтобы новое время сработало и сегодня, если оно ещё впереди
    db.update_user(m.from_user.id, send_time=f"{h:02d}:{mi:02d}", last_sent_date="")
    bot.reply_to(m, f"Дайджест будет приходить в {h:02d}:{mi:02d}.")


@bot.message_handler(commands=["tz"])
def cmd_tz(m: Message):
    tz = get_args(m)
    try:
        ZoneInfo(tz)
    except Exception:
        return bot.reply_to(m, "Неизвестный пояс. Пример: /tz Europe/Berlin")
    db.ensure_user(m.from_user.id)
    db.update_user(m.from_user.id, tz=tz)
    bot.reply_to(m, "Часовой пояс сохранён.")


def parse_topics(text: str) -> list[str]:
    """'технологии, наука; спорт' -> ['технологии', 'наука', 'спорт']"""
    return [t.strip()[:50] for t in re.split(r"[,;\n]+", text) if t.strip()]


def topics_text(topics) -> str:
    return "\n".join(f"{n}. {html.escape(t['topic'])}" for n, t in enumerate(topics, 1))


@bot.message_handler(commands=["interests"])
def cmd_interests(m: Message):
    if get_args(m):  # старая привычка: /interests текст — тоже добавляет темы
        return cmd_addinterest(m)
    topics = db.get_topics(m.from_user.id)
    if not topics:
        return bot.reply_to(m, "Тем пока нет. Добавьте: /addinterest технологии, наука")
    bot.reply_to(
        m,
        "<b>Ваши темы</b>\n" + topics_text(topics)
        + "\n\nДобавить: /addinterest ...\nУбрать: /delinterest 2",
    )


@bot.message_handler(commands=["addinterest"])
def cmd_addinterest(m: Message):
    topics = parse_topics(get_args(m))
    if not topics:
        return bot.reply_to(m, "Укажите темы через запятую: /addinterest технологии, наука")
    db.ensure_user(m.from_user.id)
    added = db.add_topics(m.from_user.id, topics)
    if added == 0:
        head = f"Новых тем нет (повторы или достигнут лимит {db.MAX_TOPICS})."
    else:
        head = f"Добавлено тем: {added}."
    bot.reply_to(m, head + "\n\n" + topics_text(db.get_topics(m.from_user.id)))


@bot.message_handler(commands=["delinterest"])
def cmd_delinterest(m: Message):
    parts = parse_topics(get_args(m))
    if not parts:
        return bot.reply_to(m, "Укажите номера или названия: /delinterest 2, 3 или /delinterest наука")
    topics = db.get_topics(m.from_user.id)  # номера относятся к этому списку
    removed = []
    for part in parts:
        if part.isdigit() and 1 <= int(part) <= len(topics):
            target = topics[int(part) - 1]
        else:
            target = next((t for t in topics if t["topic"].lower() == part.lower()), None)
        if target and db.remove_topic(m.from_user.id, target["id"]):
            removed.append(target["topic"])
    if not removed:
        return bot.reply_to(m, "Такие темы не найдены. Посмотреть список: /interests")
    left = db.get_topics(m.from_user.id)
    bot.reply_to(
        m,
        "Убрано: " + html.escape(", ".join(removed))
        + ("\n\n" + topics_text(left) if left else "\n\nТем не осталось."),
    )


@bot.message_handler(commands=["clearinterests"])
def cmd_clearinterests(m: Message):
    db.clear_topics(m.from_user.id)
    bot.reply_to(m, "Все темы удалены. Дайджест будет без фильтра по интересам.")


@bot.message_handler(commands=["digest"])
def cmd_digest(m: Message):
    db.ensure_user(m.from_user.id)
    bot.reply_to(m, "Готовлю дайджест…")
    send_digest(m.from_user.id)


# --------------------------------------------------------------------- запуск
if __name__ == "__main__":
    db.init()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logging.info("Бот запущен. Остановить: Ctrl+C")
    bot.infinity_polling(skip_pending=True)