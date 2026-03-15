import json
import contextvars
import logging
import tempfile
import os
import asyncio
import fitz
from typing import Optional

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_gigachat import GigaChat
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from gigachat import GigaChatAsyncClient, RateLimitError

from config import GIGACHAT_CREDENTIALS
from system_prompts import system_prompt_v2
from history import save_bot_message

# ================== НАСТРОЙКИ ==================
last_excel_file = {}
last_pdf_file = {}
session_store = {}
logger = logging.getLogger(__name__)

# Контекстная переменная для хранения ID текущего чата Telegram
current_chat_id_var = contextvars.ContextVar('current_chat_id', default=None)


def get_session_history(session_id: str) -> ChatMessageHistory:
    """
    Возвращает историю сообщений для указанной сессии.
    Если сессия отсутствует, создаёт новую.
    """
    if session_id not in session_store:
        session_store[session_id] = ChatMessageHistory()
    return session_store[session_id]


def create_agent_with_memory(bot):
    """
    Создаёт исполнителя агента с поддержкой истории сообщений.
    Возвращает RunnableWithMessageHistory, который автоматически управляет историей.
    """
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
    """Фабрика инструмента для отправки уведомлений в Telegram."""

    @tool
    async def send_notification(chat_id: Optional[int] = None, text: str = "") -> str:
        """
        Отправляет уведомление в указанный чат Telegram.
        Если chat_id не указан, используется текущий чат.
        """
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
    """Фабрика инструмента для чтения истории сообщений из базы данных."""

    @tool
    async def read_chat_history(
        chat_id: Optional[int] = None,
        limit: int = 300,
        days: Optional[int] = None,
        search: str = "",
    ) -> str:
        """
        Читает историю сообщений из указанного чата.
        Если chat_id не указан, используется текущий чат.
        """
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
    """Фабрика инструмента для транскрибации голосовых сообщений через GigaChat."""

    @tool
    async def transcribe_audio(file_id: str, chat_id: Optional[int] = None) -> str:
        """
        Транскрибирует голосовое сообщение по его file_id, используя хранилище GigaChat.
        """
        tmp_path = None
        try:
            # Скачивание файла от Telegram
            file = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await bot.download_file(file.file_path, tmp.name)
                tmp_path = tmp.name

            async with GigaChatAsyncClient(
                credentials=GIGACHAT_CREDENTIALS,
                verify_ssl_certs=False,
            ) as client:

                with open(tmp_path, "rb") as f:
                    uploaded = await client.aupload_file(f, purpose="general")
                audio_file_id = uploaded.id

                response = await client.achat(
                    {
                        "model": "GigaChat-Max",
                        "messages": [
                            {
                                "role": "user",
                                "content": "Распознай речь в этом аудио.",
                                "attachments": [audio_file_id],
                            }
                        ],
                        "temperature": 0.0,
                    }
                )
                text = response.choices[0].message.content.strip()

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
            logger.error(f"Ошибка транскрипции в инструменте: {e}")
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return transcribe_audio


def create_read_excel_tool(bot):
    """Фабрика инструмента для чтения Excel-файлов с помощью pandas."""
    import pandas as pd

    @tool
    async def read_excel_file(
        file_id: Optional[str] = None,
        sheet_name: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> str:
        """
        Читает данные из Excel файла и возвращает их в текстовом виде.
        Если file_id не указан, используется последний загруженный в этом чате файл.
        """
        if chat_id is None:
            chat_id = current_chat_id_var.get()
            if chat_id is None:
                return json.dumps({"error": "Не удалось определить chat_id"}, ensure_ascii=False)

        if file_id is None:
            file_id = last_excel_file.get(chat_id)
            if file_id is None:
                return json.dumps({"error": "Нет доступного Excel файла для этого чата. Сначала отправьте файл."}, ensure_ascii=False)

        tmp_path = None
        try:
            file = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                await bot.download_file(file.file_path, tmp.name)
                tmp_path = tmp.name

            if sheet_name:
                df = pd.read_excel(tmp_path, sheet_name=sheet_name)
                sheets_data = {sheet_name: df}
            else:
                sheets_data = pd.read_excel(tmp_path, sheet_name=None)

            result_lines = []
            for name, df in sheets_data.items():
                result_lines.append(f"--- Лист: {name} ---")
                if len(df) > 100:
                    result_lines.append(f"Всего строк: {len(df)}. Показаны первые 100:")
                    df_str = df.head(100).to_string()
                else:
                    df_str = df.to_string()
                result_lines.append(df_str)
                result_lines.append("")

            full_text = "\n".join(result_lines)
            if len(full_text) > 15000:
                full_text = full_text[:15000] + "\n... (обрезка по длине)"

            bot_me = await bot.me()
            await asyncio.to_thread(
                save_bot_message,
                chat_id=chat_id,
                text=f"📊 Содержимое Excel файла (file_id {file_id}) прочитано.",
                reply_to_message_id=None,
                bot_id=bot_me.id,
                bot_username=bot_me.username
            )

            return json.dumps({"success": True, "content": full_text}, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Ошибка чтения Excel: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return read_excel_file


def create_read_pdf_tool(bot):
    """Фабрика инструмента для чтения PDF-файлов с извлечением текста и OCR при необходимости."""

    @tool
    async def read_pdf_file(
        file_id: Optional[str] = None,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None,
        chat_id: Optional[int] = None,
    ) -> str:
        """
        Читает текст из PDF файла. Если текст не извлекается (сканированные страницы),
        использует GigaChat Vision для распознавания текста с изображений.
        """
        if chat_id is None:
            chat_id = current_chat_id_var.get()
            if chat_id is None:
                return json.dumps({"error": "Не удалось определить chat_id"}, ensure_ascii=False)

        target_file_id = file_id
        if target_file_id:
            try:
                await bot.get_file(target_file_id)
                logger.info(f"Используем переданный file_id: {target_file_id}")
            except Exception as e:
                logger.warning(f"Переданный file_id {target_file_id} невалиден: {e}. Пробуем последний сохранённый.")
                target_file_id = last_pdf_file.get(chat_id)
        else:
            target_file_id = last_pdf_file.get(chat_id)

        if not target_file_id:
            return json.dumps({"error": "Нет доступного PDF файла для этого чата. Сначала отправьте файл."}, ensure_ascii=False)

        tmp_pdf_path = None
        try:
            file = await bot.get_file(target_file_id)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                await bot.download_file(file.file_path, tmp.name)
                tmp_pdf_path = tmp.name

            client = await gigachat_singleton.get_async_client()

            def process_pdf_sync(pdf_path, start_page, end_page):
                """Синхронная обработка PDF: извлечение текста и подготовка страниц для OCR."""
                doc = fitz.open(pdf_path)
                total_pages = len(doc)
                start = max(1, start_page if start_page else 1)
                end = min(total_pages, end_page if end_page else total_pages)
                if start > end or start > total_pages:
                    raise ValueError(f"Неверный диапазон страниц. Всего страниц: {total_pages}")

                extracted_text_parts = []
                pages_for_ocr = []  # (page_num, image_path)

                for page_num in range(start, end + 1):
                    page = doc[page_num - 1]
                    text = page.get_text().strip()
                    if text:
                        extracted_text_parts.append(f"--- Страница {page_num} ---\n{text}")
                    else:
                        pix = page.get_pixmap(dpi=150)
                        img_fd, img_path = tempfile.mkstemp(suffix=".png")
                        os.close(img_fd)
                        pix.save(img_path)
                        pages_for_ocr.append((page_num, img_path))

                doc.close()
                return extracted_text_parts, pages_for_ocr, total_pages

            extracted_text_parts, pages_for_ocr, total_pages = await asyncio.to_thread(
                process_pdf_sync, tmp_pdf_path, page_start, page_end
            )

            ocr_results = []
            if pages_for_ocr:
                # Обработка страниц с OCR пачками по 10 изображений
                chunk_size = 10
                for i in range(0, len(pages_for_ocr), chunk_size):
                    chunk = pages_for_ocr[i:i+chunk_size]
                    attachments = []
                    temp_img_files = []

                    try:
                        for page_num, img_path in chunk:
                            temp_img_files.append(img_path)

                            # Загрузка изображения с повторными попытками при RateLimit
                            for attempt in range(3):
                                try:
                                    with open(img_path, "rb") as f:
                                        uploaded = await client.aupload_file(f, purpose="general")
                                    break
                                except RateLimitError:
                                    if attempt == 2:
                                        raise
                                    wait = 2 ** attempt
                                    logger.warning(f"Rate limit при загрузке, повтор через {wait}с")
                                    await asyncio.sleep(wait)

                            # Извлечение UUID загруженного файла
                            file_uuid = None
                            if hasattr(uploaded, 'id_'):
                                file_uuid = uploaded.id_
                            elif hasattr(uploaded, 'file_id'):
                                file_uuid = uploaded.id_
                            elif isinstance(uploaded, dict):
                                file_uuid = uploaded.get('id') or uploaded.get('file_id')
                            else:
                                for attr in dir(uploaded):
                                    if attr.endswith('id') or attr == 'uuid':
                                        file_uuid = getattr(uploaded, attr)
                                        break

                            if not file_uuid:
                                raise Exception("Не удалось получить идентификатор загруженного файла")
                            attachments.append(file_uuid)

                        payload = {
                            "model": "GigaChat-Max",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Распознай весь текст на этих изображениях и верни его. "
                                               "Для каждого изображения укажи номер страницы.",
                                    "attachments": attachments,
                                }
                            ],
                            "temperature": 0.0,
                        }

                        for attempt in range(3):
                            try:
                                response = await client.achat(payload)
                                break
                            except RateLimitError:
                                if attempt == 2:
                                    raise
                                wait = 2 ** attempt
                                logger.warning(f"Rate limit при запросе, повтор через {wait}с")
                                await asyncio.sleep(wait)

                        recognized_text = response.choices[0].message.content.strip()
                        ocr_results.append(recognized_text)

                        # Удаление временных файлов из хранилища GigaChat
                        for att_id in attachments:
                            try:
                                await client.adelete_file(att_id)
                            except Exception as e:
                                logger.warning(f"Не удалось удалить файл {att_id}: {e}")

                    finally:
                        for img_path in temp_img_files:
                            try:
                                os.unlink(img_path)
                            except OSError:
                                pass

            final_parts = extracted_text_parts.copy()
            if ocr_results:
                final_parts.append("--- Распознано с помощью OCR ---")
                final_parts.extend(ocr_results)

            full_text = "\n\n".join(final_parts) if final_parts else "Не удалось извлечь текст из PDF."
            if len(full_text) > 20000:
                full_text = full_text[:20000] + "\n... (обрезка по длине)"

            bot_me = await bot.me()
            await asyncio.to_thread(
                save_bot_message,
                chat_id=chat_id,
                text=f"📄 Содержимое PDF файла (file_id {target_file_id}) прочитано.",
                reply_to_message_id=None,
                bot_id=bot_me.id,
                bot_username=bot_me.username
            )

            return json.dumps({
                "success": True,
                "content": full_text,
                "total_pages": total_pages,
                "pages_processed": len(extracted_text_parts) + len(pages_for_ocr)
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Ошибка чтения PDF: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        finally:
            if tmp_pdf_path and os.path.exists(tmp_pdf_path):
                os.unlink(tmp_pdf_path)

    return read_pdf_file


# ================== СОЗДАНИЕ АГЕНТА ==================
def create_agent(bot):
    """
    Создаёт исполнителя агента с заданным набором инструментов и системным промптом.
    """
    tools = [
        summator,
        get_chat_id,
        create_notification_tool(bot),
        create_read_history_tool(),
        create_transcribe_tool(bot),
        create_read_excel_tool(bot),
        create_read_pdf_tool(bot),
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
    """
    Синглтон для управления общими ресурсами GigaChat:
    - асинхронный клиент для работы с файлами и прямыми запросами
    - исполнитель агента с историей
    - LLM для анализа (без инструментов)
    """
    _instance = None
    _executor = None
    _bot = None
    _analysis_llm = None
    _executor_with_history = None
    _async_client: Optional[GigaChatAsyncClient] = None

    async def get_async_client(self) -> GigaChatAsyncClient:
        """
        Возвращает асинхронный клиент GigaChat для работы с файлами и прямыми запросами.
        Клиент автоматически инициализируется при первом вызове.
        """
        if self._async_client is None:
            self._async_client = GigaChatAsyncClient(
                credentials=GIGACHAT_CREDENTIALS,
                verify_ssl_certs=False,
            )
            await self._async_client.__aenter__()
        return self._async_client

    async def close(self):
        """Закрывает асинхронный клиент GigaChat при остановке бота."""
        if self._async_client:
            await self._async_client.__aexit__(None, None, None)
            self._async_client = None

    async def get_executor(self):
        """Возвращает исполнителя агента с поддержкой истории (создаёт при первом вызове)."""
        if self._executor_with_history is None:
            self._executor_with_history = create_agent_with_memory(self._bot)
        return self._executor_with_history

    async def ainvoke_with_history(self, chat_id: int, user_message: str):
        """
        Асинхронно вызывает агента с учётом истории сообщений для указанного chat_id.
        Возвращает ответ агента.
        """
        executor = await self.get_executor()
        config = {"configurable": {"session_id": str(chat_id)}}
        result = await executor.ainvoke({"input": user_message}, config=config)
        return result["output"]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def get_analysis_llm(self):
        """
        Возвращает экземпляр GigaChat для анализа истории (без инструментов).
        """
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
        """Устанавливает экземпляр бота для использования в инструментах."""
        self._bot = bot


gigachat_singleton = GigaChatSingleton()