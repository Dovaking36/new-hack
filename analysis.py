import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

from config import ANALYSIS_INTERVAL
from history import collect_recent_messages
from agent import gigachat_singleton

logger = logging.getLogger(__name__)


async def analyze_events(chat_id: int, chat_title: str, history_text: str) -> List[Dict[str, Any]]:
    """
    Анализирует историю чата с помощью GigaChat и извлекает события для напоминаний.

    Отправляет промпт с историей чата за последние 24 часа, просит модель найти события,
    имеющие дату и время. Возвращает список событий в стандартизированном формате.

    Args:
        chat_id: Идентификатор чата Telegram.
        chat_title: Название чата (для контекста в промпте).
        history_text: Текст истории сообщений за последние 24 часа.

    Returns:
        Список событий. Каждый элемент — словарь с ключами:
            - event (str): краткое описание события
            - datetime (str): дата и время в формате "ГГГГ-ММ-ДД ЧЧ:ММ"
            - remind_before_hours (int): за сколько часов до события отправить напоминание
        Если событий нет или произошла ошибка, возвращается пустой список.
    """
    if not history_text.strip():
        return []

    prompt = f"""Ты — ассистент, который анализирует историю чата и находит важные события, о которых нужно напомнить участникам. Вот история чата "{chat_title}" за последние 24 часа:

{history_text}

Найди в этой истории упоминания о событиях (встречи, собрания, мероприятия, дедлайны, важные объявления), которые имеют конкретную дату и время. Для каждого события определи:
- Краткое описание (что за событие)
- Дата и время события в формате ГГГГ-ММ-ДД ЧЧ:ММ (например, 2026-03-14 15:00). Если указана только дата (например, "завтра" или "14 марта"), используй время 00:00.
- За сколько часов до события нужно отправить напоминание (целое число). Если не указано, используй 2 часа для событий с временем, и 24 часа для событий без времени (на весь день).

Верни результат строго в формате JSON: список объектов с полями "event", "datetime", "remind_before_hours". Если событий нет, верни пустой список [].
"""

    llm = await gigachat_singleton.get_analysis_llm()
    try:
        response = await llm.ainvoke(prompt)
        content = response.content.strip()

        # Извлечение JSON из ответа модели (может быть обёрнут в markdown или пояснения)
        json_match = re.search(r'\[\s*\{.*\}\s*\]', content, re.DOTALL)
        if json_match:
            events = json.loads(json_match.group())
        else:
            # Попытка распарсить весь ответ как JSON
            events = json.loads(content)

        # Приведение remind_before_hours к целому числу
        for ev in events:
            ev['remind_before_hours'] = int(ev.get('remind_before_hours', 2))

        return events

    except json.JSONDecodeError:
        logger.warning(f"Модель вернула невалидный JSON для чата {chat_id}: {content}")
        return []
    except Exception as e:
        logger.error(f"Ошибка при анализе событий для чата {chat_id}: {e}")
        return []


async def send_reminder(bot, chat_id: int, event: Dict[str, Any]) -> None:
    """
    Отправляет напоминание о событии в указанный чат Telegram.

    Args:
        bot: Экземпляр Telegram-бота (aiogram Bot).
        chat_id: Идентификатор чата, куда отправляется напоминание.
        event: Словарь с информацией о событии (поля 'event' и 'datetime').

    Returns:
        None
    """
    text = f"🔔 **Напоминание**\n\n{event['event']}\n\n⏰ Время события: {event['datetime']}"
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
        logger.info(f"Отправлено напоминание в чат {chat_id}: {event['event']}")
    except Exception as e:
        logger.error(f"Не удалось отправить напоминание в чат {chat_id}: {e}")