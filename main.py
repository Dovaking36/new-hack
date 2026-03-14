import asyncio
import logging
import sys
import base64
import tempfile
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram import F
from aiogram.types import ErrorEvent

from langchain_core.messages import HumanMessage

from config import TELEGRAM_BOT_TOKEN, HISTORY_DIR, ANALYSIS_INTERVAL
from agent import gigachat_singleton, current_chat_id_var, last_excel_file,last_pdf_file
from history import (
    save_user_message,
    save_bot_message,
    export_history_to_txt,
    collect_recent_messages,
    safe_json_load,
)
from analysis import analyze_events, send_reminder

# ================== НАСТРОЙКИ ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ================== ОБРАБОТЧИК ОШИБОК ==================
@dp.error()
async def errors_handler(event: ErrorEvent):
    logger.error(f"❌ Ошибка при обработке обновления {event.update}: {event.exception}", exc_info=True)
    return True

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
async def safe_save_message(message: Message):
    """Асинхронно сохраняет сообщение пользователя в историю."""
    try:
        await asyncio.to_thread(save_user_message, message)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}", exc_info=True)

# ================== ОБРАБОТЧИКИ КОМАНД ==================
@dp.message(Command("bot"))
async def cmd_bot(message: Message):
    asyncio.create_task(safe_save_message(message))
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Напишите сообщение после /bot")
        return
    user_message = parts[1]
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            start = datetime.now()

            token = current_chat_id_var.set(message.chat.id)
            try:
                answer = await gigachat_singleton.ainvoke_with_history(message.chat.id, user_message)
            finally:
                current_chat_id_var.reset(token)
            elapsed = (datetime.now() - start).total_seconds()
            logger.info(f"Обработка заняла {elapsed:.2f} сек")
            await message.reply(answer)
        except Exception as e:
            logger.error(f"Ошибка /bot: {e}", exc_info=True)
            await message.reply(f"❌ Ошибка: {e}")


@dp.message(Command("get_history"))
async def cmd_history(message: Message):
    asyncio.create_task(safe_save_message(message))

    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Только для групп")
        return

    wait_msg = await message.reply("⏳ Собираю историю...")
    try:
        chat_id = message.chat.id
        chat_title = message.chat.title or "Чат"
        txt_file = await asyncio.to_thread(export_history_to_txt, chat_id, chat_title)
        await wait_msg.edit_text("✅ Готово. Отправляю...")
        await bot.send_document(
            chat_id=message.chat.id,
            document=types.input_file.FSInputFile(txt_file)
        )
    except Exception as e:
        logger.error(f"Ошибка /get_history: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    asyncio.create_task(safe_save_message(message))

    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Только для групп")
        return

    chat_id = message.chat.id
    json_files = list(HISTORY_DIR.glob(f"chat_{chat_id}_*_*.json"))
    if not json_files:
        await message.reply("📊 Статистики нет")
        return

    def stats():
        total = 0
        users = set()
        for f in json_files:
            msgs = safe_json_load(f)
            total += len(msgs)
            for m in msgs:
                if m.get('user', {}).get('id'):
                    users.add(m['user']['id'])
        return total, users

    total, users = await asyncio.to_thread(stats)
    json_files.sort()
    first = json_files[0].stem.split('_')[-1]
    last = json_files[-1].stem.split('_')[-1]
    text = (f"📊 <b>СТАТИСТИКА</b>\n📆 {first} – {last}\n📅 Дней: {len(json_files)}\n"
            f"💬 Сообщений: {total}\n👥 Участников: {len(users)}\n📈 Среднее: {total//len(json_files) if json_files else 0}")
    await message.reply(text, parse_mode=ParseMode.HTML)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    asyncio.create_task(safe_save_message(message))
    text = (
        "📚 <b>Команды</b>\n"
        "/bot [текст] – задать вопрос\n"
        "/get_history – история группы\n"
        "/stats – статистика\n"
        "/help – помощь\n\n"
        "В личных сообщениях отвечаю на любой текст.\n"
        "В группах отвечаю, если упомянуть (@bot) или начать с 'бот'."
    )
    await message.reply(text, parse_mode=ParseMode.HTML)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    asyncio.create_task(safe_save_message(message))
    await message.reply("👋 Привет! Я бот для работы с GigaChat. Используй /help.")

# ================== ОБРАБОТЧИК ФОТО ==================
@dp.message(F.photo)
async def handle_photo(message: Message):
    asyncio.create_task(safe_save_message(message))
    photo = message.photo[-1]
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            file = await bot.get_file(photo.file_id)
            file_content = await bot.download_file(file.file_path)
            base64_str = base64.b64encode(file_content.read()).decode('utf-8')

            prompt = message.caption or "Опиши это изображение подробно на русском языке. Если на изображении есть текст, извлеки его."

            llm = await gigachat_singleton.get_analysis_llm()


            human_msg = HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_str}"}}
                ]
            )

            response = await llm.agenerate([[human_msg]])
            answer = response.generations[0][0].text

            await message.reply(answer)

            bot_me = await bot.me()
            await asyncio.to_thread(
                save_bot_message,
                chat_id=message.chat.id,
                text=answer,
                reply_to_message_id=message.message_id,
                bot_id=bot_me.id,
                bot_username=bot_me.username,
            )

            logger.info(f"Обработано фото от {message.from_user.id}")
        except Exception as e:
            logger.error(f"Ошибка при обработке фото: {e}", exc_info=True)
            await message.reply("❌ Не удалось обработать изображение.")

# ================== ОБРАБОТЧИК ГОЛОСОВЫХ СООБЩЕНИЙ ==================
# main.py
@dp.message(F.voice)
async def handle_voice(message: Message):
    asyncio.create_task(safe_save_message(message))
    voice = message.voice
    tmp_path = None  

    try:

        file = await bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await bot.download_file(file.file_path, tmp.name)
            tmp_path = tmp.name

        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):

            client = await gigachat_singleton.get_async_client()


            with open(tmp_path, "rb") as f:
                uploaded = await client.aupload_file(f, purpose="general")
            audio_file_id = uploaded.id_


            response = await client.achat({
                "model": "GigaChat-Max",
                "messages": [
                    {
                        "role": "user",
                        "content": "Распознай речь в этом аудио.",
                        "attachments": [audio_file_id],
                    }
                ],
                "temperature": 0.0,
            })

            transcribed_text = response.choices[0].message.content.strip()


            # await client.delete_file(file_id)

        if not transcribed_text:
            transcribed_text = "[Не удалось распознать речь]"

        await message.reply(f"📝 Распознано: {transcribed_text}")


        bot_me = await bot.me()
        await asyncio.to_thread(
            save_bot_message,
            chat_id=message.chat.id,
            text=f"🎤 Голосовое сообщение расшифровано: {transcribed_text}",
            reply_to_message_id=message.message_id,
            bot_id=bot_me.id,
            bot_username=bot_me.username,
        )

    except Exception as e:
        logger.error(f"Ошибка транскрипции через GigaChat: {e}", exc_info=True)
        await message.reply("❌ Не удалось распознать речь.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@dp.message(F.document)
async def handle_document(message: Message):
    asyncio.create_task(safe_save_message(message))
    document = message.document

    logger.info(f"Получен документ: {document.file_name}, MIME: {document.mime_type}, размер: {document.file_size}")

    # MIME-типы для Excel
    excel_mime_types = [
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',  # .xlsx
        'application/vnd.ms-excel',                                           # .xls
        'application/vnd.oasis.opendocument.spreadsheet',                    # .ods
    ]
    # MIME-типы для PDF
    pdf_mime_types = [
        'application/pdf',
    ]

    if document.mime_type in excel_mime_types:
        last_excel_file[message.chat.id] = document.file_id
        await message.reply(
            "📊 Excel файл получен и сохранён. "
            "Теперь вы можете задать вопрос о нём через /bot или упомянуть меня, "
            "используя инструмент `read_excel_file`."
        )
    elif document.mime_type in pdf_mime_types:
        last_pdf_file[message.chat.id] = document.file_id
        await message.reply(
            "📄 PDF файл получен и сохранён. "
            "Теперь вы можете задать вопрос о нём через /bot или упомянуть меня, "
            "используя инструмент `read_pdf_file`."
        )
    else:
        await message.reply("Я пока умею работать только с Excel и PDF файлами. Пожалуйста, отправьте .xlsx, .xls или .pdf.")
# ================== УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ТЕКСТА ==================
@dp.message()
async def handle_all_text(message: Message):
    logger.info(f"📩 Входящее сообщение: chat={message.chat.id}, user={message.from_user.id}, text={message.text[:50] if message.text else 'нет текста'}")
    asyncio.create_task(safe_save_message(message))

    if not message.text:
        return

    is_private = message.chat.type == 'private'
    is_mention = False
    if message.chat.type in ['group', 'supergroup']:
        bot_user = await bot.me()
        bot_username = bot_user.username.lower()
        text_lower = message.text.lower()
        if f"@{bot_username}" in text_lower or text_lower.startswith("бот"):
            is_mention = True

    if is_private or is_mention:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            try:
                token = current_chat_id_var.set(message.chat.id)
                try:
                    answer = await gigachat_singleton.ainvoke_with_history(message.chat.id, message.text)
                finally:
                    current_chat_id_var.reset(token)
                await message.reply(answer)
            except Exception as e:
                logger.error(f"Ошибка ответа: {e}", exc_info=True)
                await message.reply("❌ Произошла ошибка")


# ================== ПРИВЕТСТВИЕ НОВЫХ УЧАСТНИКОВ ==================
@dp.message(F.new_chat_members)
async def welcome(message: Message):
    bot_user = await bot.me()
    for member in message.new_chat_members:
        if member.id == bot_user.id:
            await message.answer(
                "👋 Привет! Я буду сохранять историю.\n"
                "Упомяните меня @bot, чтобы поговорить."
            )

# ================== ФОНОВАЯ ЗАДАЧА АНАЛИЗА ==================
async def periodic_analysis():
    """Фоновая задача: каждые ANALYSIS_INTERVAL секунд анализирует историю чатов."""
    await asyncio.sleep(60)
    while True:
        try:
            logger.info("🔄 Запуск периодического анализа истории чатов")
            chat_ids = set()
            for file in HISTORY_DIR.glob("chat_*_*.json"):
                parts = file.stem.split('_')
                if len(parts) >= 2:
                    try:
                        chat_id = int(parts[1])
                        chat_ids.add(chat_id)
                    except ValueError:
                        continue

            for chat_id in chat_ids:
                try:
                    history_text = await asyncio.to_thread(collect_recent_messages, chat_id, 24)
                    if not history_text:
                        continue

                    chat_title = "Чат"
                    events = await analyze_events(chat_id, chat_title, history_text)

                    now = datetime.now()
                    for event in events:
                        try:
                            event_datetime_str = event['datetime']
                            if len(event_datetime_str) == 10:
                                event_datetime_str += " 00:00"
                            event_time = datetime.strptime(event_datetime_str, "%Y-%m-%d %H:%M")
                        except Exception as e:
                            logger.warning(f"Не удалось распарсить дату события: {event['datetime']}, ошибка: {e}")
                            continue

                        remind_before = timedelta(hours=event.get('remind_before_hours', 2))
                        remind_time = event_time - remind_before
                        if remind_time <= now <= remind_time + timedelta(seconds=ANALYSIS_INTERVAL):
                            await send_reminder(bot, chat_id, event)
                        elif remind_time > now and remind_time - now < timedelta(seconds=ANALYSIS_INTERVAL):
                            await send_reminder(bot, chat_id, event)
                        elif remind_time < now and event_time > now:
                            await send_reminder(bot, chat_id, event)

                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"Ошибка при обработке чата {chat_id}: {e}")

        except Exception as e:
            logger.error(f"Критическая ошибка в periodic_analysis: {e}")

        await asyncio.sleep(ANALYSIS_INTERVAL)

# ================== ЗАПУСК ==================
async def on_startup():
    logger.info("🚀 Бот запускается...")
    gigachat_singleton.set_bot(bot)
    try:
        await gigachat_singleton.get_executor()
        logger.info("✅ Агент GigaChat готов")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации агента: {e}")
        raise
    asyncio.create_task(periodic_analysis())
    logger.info("✅ Фоновая задача анализа запущена")

async def on_shutdown():
    logger.info("👋 Бот останавливается...")
    await gigachat_singleton.close()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
