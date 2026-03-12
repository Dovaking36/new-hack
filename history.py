import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from config import HISTORY_DIR, TXT_EXPORT_DIR

# ---------- Безопасная работа с JSON ----------
def safe_json_load(filepath: Path) -> List[Dict[str, Any]]:
    """Безопасно загружает JSON, восстанавливая повреждённые файлы."""
    try:
        if not filepath.exists():
            return []
        content = filepath.read_text(encoding='utf-8').strip()
        if not content:
            return []
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Попытка восстановить: обрезать до последней закрывающей скобки
            last_bracket = content.rfind(']')
            if last_bracket > 0:
                content = content[:last_bracket+1]
                try:
                    return json.loads(content)
                except:
                    pass
            # Если не получилось, создаём бэкап
            backup_path = filepath.with_suffix('.json.bak')
            filepath.rename(backup_path)
            return []
    except Exception:
        return []

def safe_json_save(filepath: Path, data: List[Dict[str, Any]]) -> bool:
    """Безопасно сохраняет JSON с временным файлом."""
    temp_path = filepath.with_suffix('.tmp')
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        temp_path.replace(filepath)
        return True
    except Exception:
        try:
            temp_path.unlink()
        except:
            pass
        return False

# ---------- Сохранение сообщений ----------
def _get_history_file(chat_id: int, date: Optional[datetime] = None) -> Path:
    """Возвращает путь к файлу истории для данного чата и даты."""
    if date is None:
        date = datetime.now()
    date_str = date.strftime("%Y-%m-%d")
    chat_type = 'private' if chat_id > 0 else 'group'
    filename = f"chat_{chat_id}_{chat_type}_{date_str}.json"
    return HISTORY_DIR / filename

def save_user_message(message) -> None:
    """Сохраняет сообщение пользователя в JSON-историю (синхронно)."""
    try:
        if not message.text:
            return
        chat_id = message.chat.id
        date = message.date.replace(tzinfo=None)
        filepath = _get_history_file(chat_id, date)

        messages = safe_json_load(filepath)

        user = message.from_user
        message_data = {
            "id": message.message_id,
            "timestamp": message.date.isoformat(),
            "unix_time": int(message.date.timestamp()),
            "user": {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            },
            "chat": {
                "id": chat_id,
                "type": message.chat.type,
                "title": getattr(message.chat, "title", None)
            },
            "text": message.text
        }

        messages.append(message_data)
        safe_json_save(filepath, messages)
    except Exception as e:
        print(f"Ошибка сохранения сообщения пользователя: {e}")

def save_bot_message(
    chat_id: int,
    text: str,
    reply_to_message_id: Optional[int],
    bot_id: int,
    bot_username: str
) -> None:
    """Сохраняет сообщение бота в JSON-историю (синхронно)."""
    try:
        date = datetime.now()
        filepath = _get_history_file(chat_id, date)

        messages = safe_json_load(filepath)

        message_data = {
            "id": f"bot_{int(time.time())}_{reply_to_message_id or 0}",
            "timestamp": date.isoformat(),
            "unix_time": int(date.timestamp()),
            "user": {
                "id": bot_id,
                "username": bot_username,
                "first_name": "Bot",
                "last_name": ""
            },
            "chat": {
                "id": chat_id,
                "type": 'private' if chat_id > 0 else 'group',
                "title": None
            },
            "text": text,
            "reply_to_message_id": reply_to_message_id
        }

        messages.append(message_data)
        safe_json_save(filepath, messages)
    except Exception as e:
        print(f"Ошибка сохранения сообщения бота: {e}")

# ---------- Чтение истории ----------
def load_chat_history(
    chat_id: int,
    limit: int = 50,
    days: Optional[int] = None,
    search: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Загружает историю сообщений из файлов.
    Возвращает список сообщений (каждое — словарь).
    """
    limit = min(limit, 200)
    search_lower = search.lower() if search else None

    pattern = f"chat_{chat_id}_*_*.json"
    files = sorted(HISTORY_DIR.glob(pattern))

    if not files:
        return []

    all_msgs = []
    cutoff_timestamp = None
    if days:
        cutoff_timestamp = (datetime.now() - timedelta(days=days)).timestamp()

    for file in reversed(files):
        try:
            msgs = safe_json_load(file)
            if days and cutoff_timestamp:
                msgs = [m for m in msgs if m.get('unix_time', 0) > cutoff_timestamp]
            all_msgs.extend(msgs)
            if len(all_msgs) >= limit * 2:
                break
        except Exception:
            continue

    if not all_msgs:
        return []

    all_msgs.sort(key=lambda x: x.get('unix_time', 0))
    recent_msgs = all_msgs[-limit:]

    if search_lower:
        recent_msgs = [
            m for m in recent_msgs
            if search_lower in m.get('text', '').lower()
        ]

    return recent_msgs

def format_messages_for_display(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Преобразует сообщения в удобный для вывода формат."""
    result = []
    for msg in messages:
        user_info = msg.get('user', {})
        username = user_info.get('username') or user_info.get('first_name', 'Unknown')
        timestamp = msg.get('timestamp', '')
        text = msg.get('text', '')
        result.append({
            "time": timestamp,
            "user": username,
            "text": text,
        })
    return result

def collect_recent_messages(chat_id: int, hours: int = 24) -> str:
    """
    Собирает сообщения за последние N часов и возвращает их в виде текста.
    Используется для анализа.
    """
    cutoff_time = datetime.now() - timedelta(hours=hours)
    cutoff_timestamp = cutoff_time.timestamp()

    pattern = f"chat_{chat_id}_*_*.json"
    files = sorted(HISTORY_DIR.glob(pattern))
    if not files:
        return ""

    lines = []
    for file in files:
        try:
            msgs = safe_json_load(file)
            for msg in msgs:
                msg_time = msg.get('unix_time', 0)
                if msg_time > cutoff_timestamp:
                    dt = datetime.fromtimestamp(msg_time).strftime('%Y-%m-%d %H:%M')
                    user = msg['user'].get('username') or msg['user']['first_name']
                    text = msg.get('text', '')
                    if text:
                        lines.append(f"[{dt}] {user}: {text}")
        except Exception:
            continue

    return "\n".join(lines)

def export_history_to_txt(chat_id: int, chat_title: str) -> Path:
    """
    Экспортирует всю историю чата в текстовый файл.
    Возвращает путь к созданному файлу.
    """
    json_files = sorted(HISTORY_DIR.glob(f"chat_{chat_id}_*_*.json"))
    if not json_files:
        raise ValueError("Нет файлов истории")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = "".join(c for c in chat_title if c.isalnum() or c in (' ', '-', '_')).strip()
    txt_file = TXT_EXPORT_DIR / f"{safe_title}_history_{timestamp}.txt"

    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write(f"История чата {chat_title}\nID: {chat_id}\n\n")
        total = 0
        for jf in json_files:
            date = jf.stem.split('_')[-1]
            f.write(f"--- {date} ---\n")
            msgs = safe_json_load(jf)
            for m in msgs:
                if m.get('text'):
                    username = m['user'].get('username') or m['user']['first_name']
                    tm = m['timestamp'][11:19] if m.get('timestamp') else '00:00:00'
                    f.write(f"[{tm}] {username}: {m['text']}\n")
                    total += 1
        f.write(f"\nВсего сообщений: {total}")

    return txt_file