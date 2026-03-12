import json
import asyncio
import contextvars
import logging
import tempfile
import os
import base64
from pathlib import Path
from datetime import datetime
from typing import Optional

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain.tools import tool
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_gigachat import GigaChat
from langchain_core.messages import HumanMessage

from system_prompts import system_prompt_v2
from history_utils import save_bot_message_to_history

# ================== НАСТРОЙКИ ==================
CREDENTIALS = "MDE5Y2Q2OTYtMTk2ZC03YzVjLTgxZTQtOTk5NjhlNWRjYWFlOjFjZWU1YjI4LWRiYWUtNGIxMS05NGMyLTBlYmQ4NWEyMTVhYw=="
HISTORY_DIR = "chat_history"

logger = logging.getLogger(__name__)

# Переменная контекста для хранения ID текущего чата
current_chat_id_var = contextvars.ContextVar('current_chat_id', default=None)

# ================== ИНСТРУМЕНТЫ ==================

@tool
async def summator(a: float, b: float) -> str:
    """Складывает два числа и возвращает результат."""
    result = a + b
    return json.dumps({"result": result, "expression": f"{a} + {b}"}, ensure_ascii=False)

@tool
async def get_chat_id() -> str:
    """Возвращает ID текущего чата Telegram."""
    chat_id = current_chat_id_var.get()
    if chat_id:
        return json.dumps({"chat_id": chat_id}, ensure_ascii=False)
    else:
        return json.dumps({"error": "Не удалось определить chat_id"}, ensure_ascii=False)

def create_notification_tool(bot):
    @tool
    async def send_notification(chat_id: Optional[int] = None, text: str = "") -> str:
        """Отправляет уведомление в указанный чат Telegram. Если chat_id не указан, используется текущий чат."""
        if not text:
            return json.dumps({"error": "Не указан текст уведомления"}, ensure_ascii=False)

        if chat_id is None:
            chat_id = current_chat_id_var.get()
            if chat_id is None:
                return json.dumps({"error": "Не указан chat_id и не удалось определить из контекста"}, ensure_ascii=False)

        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return json.dumps({"status": "ok", "message": "Уведомление отправлено"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
    return send_notification

def create_read_history_tool(history_dir: str = HISTORY_DIR):
    history_path = Path(history_dir)

    @tool
    async def read_chat_history(
        chat_id: Optional[int] = None,
        limit: int = 50,
        days: Optional[int] = None,
        search: str = "",
    ) -> str:
        """Читает историю сообщений из указанного чата. Если chat_id не указан, используется текущий чат."""
        if chat_id is None:
            chat_id = current_chat_id_var.get()
            if chat_id is None:
                return json.dumps({"error": "Не указан chat_id и не удалось определить из контекста"}, ensure_ascii=False)

        limit = min(limit, 200)
        search = search.lower()
        pattern = f"chat_{chat_id}_*_*.json"
        files = sorted(history_path.glob(pattern))
        if not files:
            return json.dumps({"message": f"История для чата {chat_id} не найдена"}, ensure_ascii=False)

        all_msgs = []
        cutoff_timestamp = None
        if days:
            cutoff_timestamp = datetime.now().timestamp() - days * 24 * 3600

        for file in reversed(files):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    msgs = json.load(f)
                if days and cutoff_timestamp:
                    msgs = [m for m in msgs if m.get('unix_time', 0) > cutoff_timestamp]
                all_msgs.extend(msgs)
                if len(all_msgs) >= limit * 2:
                    break
            except Exception:
                continue

        if not all_msgs:
            return json.dumps({"message": "Нет сообщений за указанный период"}, ensure_ascii=False)

        all_msgs.sort(key=lambda x: x.get('unix_time', 0))
        recent_msgs = all_msgs[-limit:]

        if search:
            recent_msgs = [m for m in recent_msgs if search in m.get('text', '').lower()]
            if not recent_msgs:
                return json.dumps({"message": f"Сообщений с '{search}' не найдено"}, ensure_ascii=False)

        result = {
            "chat_id": chat_id,
            "total_messages_found": len(recent_msgs),
            "period": f"последние {limit} сообщений",
            "messages": [],
        }
        if days:
            result["period"] = f"последние {days} дней"

        for msg in recent_msgs:
            user_info = msg.get('user', {})
            username = user_info.get('username') or user_info.get('first_name', 'Unknown')
            timestamp = msg.get('timestamp', '')
            text = msg.get('text', '')
            result["messages"].append({
                "time": timestamp,
                "user": username,
                "text": text,
            })
        return json.dumps(result, ensure_ascii=False, indent=2)
    return read_chat_history

def create_transcribe_tool(bot):
    @tool
    async def transcribe_audio(file_id: str, chat_id: Optional[int] = None) -> str:
        """
        Транскрибирует голосовое сообщение по его file_id.
        Параметры:
            file_id (str): идентификатор файла в Telegram.
            chat_id (int, optional): ID чата для сохранения результата в историю.
        Возвращает распознанный текст.
        """
        try:
            # Получаем файл
            file = await bot.get_file(file_id)
            file_path = file.file_path

            # Скачиваем во временный файл
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await bot.download_file(file_path, tmp.name)
                tmp_path = tmp.name

            # Читаем и конвертируем в base64
            with open(tmp_path, "rb") as f:
                audio_base64 = base64.b64encode(f.read()).decode('utf-8')

            # Формируем запрос к GigaChat
            prompt = "Распознай речь в этом аудио."
            human_msg = HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "audio_url", "audio_url": {"url": f"data:audio/ogg;base64,{audio_base64}"}}
                ]
            )

            llm = GigaChat(
                credentials=CREDENTIALS,
                verify_ssl_certs=False,
                model="GigaChat-2-Multimodal",
                temperature=0.0,
                max_tokens=1024,
                auto_upload_audios=True,
            )
            response = await llm.agenerate([[human_msg]])
            text = response.generations[0][0].text.strip()

            # Если указан chat_id, сохраняем результат в историю
            if chat_id:
                bot_me = await bot.me()
                await save_bot_message_to_history(
                    chat_id=chat_id,
                    text=f"🎤 Транскрипция аудио (file_id {file_id}): {text}",
                    reply_to_message_id=None,
                    bot_id=bot_me.id,
                    bot_username=bot_me.username
                )

            return json.dumps({"success": True, "text": text}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Ошибка транскрипции: {e}")
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        finally:
            if 'tmp_path' in locals():
                os.unlink(tmp_path)

    return transcribe_audio

# ================== СОЗДАНИЕ АГЕНТА ==================
def create_agent(bot):
    tools = [
        summator,
        get_chat_id,
        create_notification_tool(bot),
        create_read_history_tool(HISTORY_DIR),
        create_transcribe_tool(bot),
    ]

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt_v2),
        ("placeholder", "{chat_history}"),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    llm = GigaChat(
        credentials=CREDENTIALS,
        verify_ssl_certs=False,
        model="GigaChat",
        temperature=0.7,
        max_tokens=1024,
    )

    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True, handle_parsing_errors=True)
    return executor

# ================== СИНГЛТОН ==================
class GigaChatSingleton:
    _instance = None
    _executor = None
    _bot = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def set_bot(self, bot):
        self._bot = bot

    async def get_executor(self):
        if self._executor is None:
            self._executor = create_agent(self._bot)
        return self._executor

    async def close(self):
        pass

gigachat_singleton = GigaChatSingleton()