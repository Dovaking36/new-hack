
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Настройки (можно передавать параметрами, но для простоты оставим константы)
HISTORY_DIR = "chat_history"

# ---------- Безопасная работа с JSON ----------
def safe_json_load(filepath):
    """Безопасно загружает JSON, восстанавливая повреждённые файлы."""
    try:
        if not os.path.exists(filepath):
            return []
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return []
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            # Попытка восстановить
            last_bracket = content.rfind(']')
            if last_bracket > 0:
                content = content[:last_bracket+1]
                try:
                    return json.loads(content)
                except:
                    pass
            # Если не получилось, создаём бэкап
            backup_path = filepath + '.bak'
            try:
                os.rename(filepath, backup_path)
            except:
                pass
            return []
    except Exception:
        return []

def safe_json_save(filepath, data):
    """Безопасно сохраняет JSON с временным файлом."""
    temp_path = filepath + '.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
        return True
    except Exception:
        try:
            os.remove(temp_path)
        except:
            pass
        return False

# ---------- Сохранение сообщений ----------
async def save_bot_message_to_history(
    chat_id: int,
    text: str,
    reply_to_message_id: int,
    bot_id: int,
    bot_username: str
):
    """Сохраняет сообщение бота в JSON историю."""
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        chat_type = 'private' if chat_id > 0 else 'group'
        filename = f"{HISTORY_DIR}/chat_{chat_id}_{chat_type}_{date_str}.json"

        messages = safe_json_load(filename)

        message_data = {
            "id": f"bot_{int(time.time())}_{reply_to_message_id}",
            "timestamp": datetime.now().isoformat(),
            "unix_time": int(datetime.now().timestamp()),
            "user": {
                "id": bot_id,
                "username": bot_username,
                "first_name": "Bot",
                "last_name": ""
            },
            "chat": {
                "id": chat_id,
                "type": chat_type,
                "title": None
            },
            "text": text,
            "reply_to_message_id": reply_to_message_id
        }

        messages.append(message_data)
        safe_json_save(filename, messages)
    except Exception as e:
        # Здесь нельзя использовать logger из-за возможных циклов, можно print или передавать логгер параметром
        print(f"Ошибка сохранения ответа бота: {e}")

# Можно добавить и другие общие функции, например, для логирования