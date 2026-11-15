"""Обёртка над Groq API: подбор RSS по названию и фильтрация/обобщение новостей."""
import json
import os
import re
import time

from groq import Groq, RateLimitError

import rss

client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def _chat(system: str, user: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
            return resp.choices[0].message.content or ""
        except RateLimitError:
            if attempt == retries:
                raise
            time.sleep(5 * (attempt + 1))  # TODO: переключение на запасного провайдера
    return ""


def _parse_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (на случай ```json-обёрток)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------- 1. Подбор RSS по названию сайта ----------
def find_feed(name: str) -> tuple[str, str] | None:
    """Возвращает (название, rss_url) или None. LLM даёт кандидатов, код их проверяет."""
    system = (
        "Ты помогаешь найти RSS-ленты новостных сайтов. Отвечай ТОЛЬКО JSON вида "
        '{"site": "https://главная-страница", "feeds": ["https://...", "https://..."]}. '
        "Если не уверен в адресе, оставь feeds пустым списком. Ничего не выдумывай."
    )
    data = _parse_json(_chat(system, f"Новостной сайт: {name}"))

    # 1) пробуем адреса, предложенные моделью
    for url in data.get("feeds", [])[:3]:
        title = rss.check_feed(url)
        if title:
            return title, url

    # 2) автообнаружение ленты на главной странице сайта
    site = data.get("site")
    if site:
        url = rss.discover_feed(site)
        if url:
            return (rss.check_feed(url) or name), url
    return None


# ---------- 2. Фильтрация и обобщение ----------
def make_digest(items: list[dict], interests: str, top_n: int = 10) -> list[dict]:
    """Выбирает до top_n записей по интересам. Возвращает [{'item': {...}, 'summary': str}].

    Ссылки модель НЕ генерирует: она возвращает только номера записей,
    а ссылки берутся из исходных данных, поэтому выдуманных URL быть не может.
    """
    listing = "\n".join(
        f"{i}. [{it['source']}] {it['title']} — {it['summary']}" for i, it in enumerate(items)
    )
    system = (
        "Ты редактор новостного дайджеста. Из списка выбери самые важные и интересные "
        f"записи (не более {top_n}), учитывая интересы пользователя. Для каждой напиши "
        "краткое резюме на русском (одно предложение). Отвечай ТОЛЬКО JSON вида "
        '{"items": [{"i": 0, "summary": "..."}]}.'
    )
    user = f"Интересы пользователя: {interests or 'не указаны'}\n\nНовости:\n{listing}"
    data = _parse_json(_chat(system, user))

    result = []
    for row in data.get("items", []):
        idx = row.get("i")
        if isinstance(idx, int) and 0 <= idx < len(items):
            result.append({"item": items[idx], "summary": str(row.get("summary", "")).strip()})
    return result[:top_n]
