import json
import contextvars
import logging
import tempfile
import os
import base64
from typing import Optional

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_gigachat import GigaChat
from langchain_core.messages import HumanMessage
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory

from config import GIGACHAT_CREDENTIALS
from system_prompts import system_prompt_v2
from history import save_bot_message

# ================== НАСТРОЙКИ ==================
session_store = {}
logger = logging.getLogger(__name__)

# Переменная контекста для хранения ID текущего чата
current_chat_id_var = contextvars.ContextVar('current_chat_id', default=None)

def get_session_history(session_id: str) -> ChatMessageHistory:
    if session_id not in session_store:
        session_store[session_id] = ChatMessageHistory()
    return session_store[session_id]

def create_agent_with_memory(bot):
    executor = create_agent(bot)
    with_history = RunnableWithMessageHistory(
        executor,
        get_session_history,
        input_messages_key="input",
        history_messages_key="chat_history",
    )
    return with_history

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

def create_read_history_tool():
    @tool
    async def read_chat_history(
        chat_id: Optional[int] = None,
        limit: int = 50,
        days: Optional[int] = None,
        search: str = "",
    ) -> str:
        """Читает историю сообщений из указанного чата. Если chat_id не указан, используется текущий чат."""
        from history import load_chat_history, format_messages_for_display

        if chat_id is None:
            chat_id = current_chat_id_var.get()
            if chat_id is None:
                return json.dumps({"error": "Не указан chat_id и не удалось определить из контекста"}, ensure_ascii=False)

        messages = load_chat_history(chat_id, limit, days, search)
        if not messages:
            return json.dumps({"message": "Нет сообщений за указанный период"}, ensure_ascii=False)

        formatted = format_messages_for_display(messages)
        result = {
            "chat_id": chat_id,
            "total_messages_found": len(formatted),
            "period": f"последние {limit} сообщений",
            "messages": formatted,
        }
        if days:
            result["period"] = f"последние {days} дней"
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
            file = await bot.get_file(file_id)
            file_path = file.file_path

            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await bot.download_file(file_path, tmp.name)
                tmp_path = tmp.name

            with open(tmp_path, "rb") as f:
                audio_base64 = base64.b64encode(f.read()).decode('utf-8')

            prompt = "Распознай речь в этом аудио."
            human_msg = HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "audio_url", "audio_url": {"url": f"data:audio/ogg;base64,{audio_base64}"}}
                ]
            )

            llm = GigaChat(
                credentials=GIGACHAT_CREDENTIALS,
                verify_ssl_certs=False,
                model="GigaChat-2-Multimodal",
                temperature=0.0,
                max_tokens=1024,
                auto_upload_audios=True,
            )
            response = await llm.agenerate([[human_msg]])
            text = response.generations[0][0].text.strip()

            if chat_id:
                bot_me = await bot.me()
                save_bot_message(
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
        create_read_history_tool(),
        create_transcribe_tool(bot),
    ]

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt_v2),
        ("placeholder", "{chat_history}"),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    llm = GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
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
    _analysis_llm = None
    _executor_with_history = None

    async def get_executor(self):
        if self._executor_with_history is None:
            self._executor_with_history = create_agent_with_memory(self._bot)
        return self._executor_with_history

    async def ainvoke_with_history(self, chat_id: int, user_message: str):
        executor = await self.get_executor()
        config = {"configurable": {"session_id": str(chat_id)}}
        result = await executor.ainvoke({"input": user_message}, config=config)
        return result["output"]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def get_analysis_llm(self):
        """Возвращает экземпляр GigaChat для анализа истории (без инструментов)."""
        if self._analysis_llm is None:
            self._analysis_llm = GigaChat(
                credentials=GIGACHAT_CREDENTIALS,
                verify_ssl_certs=False,
                model="GigaChat-Max",
                temperature=0.0,
                max_tokens=2000,
                auto_upload_images=True
            )
        return self._analysis_llm

    def set_bot(self, bot):
        self._bot = bot

    async def close(self):
        pass

gigachat_singleton = GigaChatSingleton()