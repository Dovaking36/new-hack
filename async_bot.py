import time
import asyncio
import json
import logging
import os
import sys
import base64
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram import F
from aiogram.types import ErrorEvent

from langchain_gigachat import GigaChat
from langchain_core.messages import HumanMessage

from agent import gigachat_singleton, current_chat_id_var
from history_utils import safe_json_load, safe_json_save, save_bot_message_to_history

# ================== НАСТРОЙКИ ==================
CREDENTIALS = "8562857508:AAFW3w8W2u44fYte2LZCoorZ9pfOgieYKkc"
HISTORY_DIR = "chat_history"
TXT_EXPORT_DIR = "txt_exports"

Path(HISTORY_DIR).mkdir(exist_ok=True)
Path(TXT_EXPORT_DIR).mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot = Bot(token=CREDENTIALS)
dp = Dispatcher()

# ================== ОБРАБОТЧИК ОШИБОК ==================
@dp.error()
async def errors_handler(event: ErrorEvent):
    logger.error(f"❌ Ошибка при обработке обновления {event.update}: {event.exception}", exc_info=True)
    return True

# ================== СОХРАНЕНИЕ В JSON ==================
def save_message_to_json_sync(message: Message):
    """Сохраняет сообщение в JSON с защитой от повреждений"""
    try:
        if not message.text:
            return
        chat_id = message.chat.id
        chat_type = message.chat.type
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{HISTORY_DIR}/chat_{chat_id}_{chat_type}_{date_str}.json"

        messages = safe_json_load(filename)

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
                "type": chat_type,
                "title": getattr(message.chat, "title", None)
            },
            "text": message.text
        }

        messages.append(message_data)
        if safe_json_save(filename, messages):
            logger.debug(f"Сохранено в {filename}")
        else:
            logger.error(f"Не удалось сохранить сообщение в {filename}")
    except Exception as e:
        logger.error(f"Критическая ошибка в save_message_to_json_sync: {e}", exc_info=True)

async def safe_save_message(message: Message):
    """Асинхронно запускает сохранение в потоке"""
    try:
        await asyncio.to_thread(save_message_to_json_sync, message)
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
            executor = await gigachat_singleton.get_executor()
            start = time.monotonic()

            token = current_chat_id_var.set(message.chat.id)
            try:
                result = await executor.ainvoke({"input": user_message})
                answer = result["output"]
            finally:
                current_chat_id_var.reset(token)

            logger.info(f"Обработка заняла {time.monotonic() - start:.2f} сек")
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
        chat_title = "".join(c for c in message.chat.title if c.isalnum() or c in (' ', '-', '_')).strip()
        json_files = await asyncio.to_thread(
            lambda: list(Path(HISTORY_DIR).glob(f"chat_{chat_id}_*_*.json"))
        )
        if not json_files:
            await wait_msg.edit_text("❌ История пуста")
            return

        json_files.sort()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt_file = f"{TXT_EXPORT_DIR}/{chat_title}_history_{timestamp}.txt"

        def write_txt():
            total = 0
            with open(txt_file, 'w', encoding='utf-8') as f:
                f.write(f"История группы {message.chat.title}\nID: {chat_id}\n")
                for jf in json_files:
                    date = str(jf).split('_')[-1].replace('.json', '')
                    f.write(f"\n--- {date} ---\n")
                    msgs = safe_json_load(jf)
                    for m in msgs:
                        if m.get('text'):
                            username = m['user'].get('username') or m['user']['first_name']
                            tm = m['timestamp'][11:19] if m.get('timestamp') else '00:00:00'
                            f.write(f"[{tm}] {username}: {m['text']}\n")
                            total += 1
            return total

        total = await asyncio.to_thread(write_txt)
        await wait_msg.edit_text(f"✅ Собрано {total} сообщений. Отправляю...")
        with open(txt_file, 'rb') as f:
            await bot.send_document(chat_id=message.chat.id, document=types.input_file.FSInputFile(txt_file))
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
    json_files = await asyncio.to_thread(
        lambda: list(Path(HISTORY_DIR).glob(f"chat_{chat_id}_*_*.json"))
    )
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
    first = str(json_files[0]).split('_')[-1].replace('.json', '')
    last = str(json_files[-1]).split('_')[-1].replace('.json', '')
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
            # Скачиваем фото
            file = await bot.get_file(photo.file_id)
            file_content = await bot.download_file(file.file_path)
            base64_str = base64.b64encode(file_content.read()).decode('utf-8')

            prompt = message.caption or "Опиши это изображение подробно на русском языке. Если на изображении есть текст, извлеки его."

            llm = GigaChat(
                credentials="MDE5Y2Q2OTYtMTk2ZC03YzVjLTgxZTQtOTk5NjhlNWRjYWFlOjFjZWU1YjI4LWRiYWUtNGIxMS05NGMyLTBlYmQ4NWEyMTVhYw==",
                verify_ssl_certs=False,
                model="GigaChat-Max",
                temperature=0.7,
                max_tokens=1024,
                auto_upload_images=True
            )

            human_msg = HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_str}"}
                    }
                ]
            )

            response = await llm.agenerate([[human_msg]])
            answer = response.generations[0][0].text

            await message.reply(answer)

            # Сохраняем ответ бота в историю
            bot_me = await bot.me()
            await save_bot_message_to_history(
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
@dp.message(F.voice)
async def handle_voice(message: Message):
    asyncio.create_task(safe_save_message(message))

    voice = message.voice
    file = await bot.get_file(voice.file_id)
    file_path = file.file_path

    # Скачиваем аудио во временный файл
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await bot.download_file(file_path, tmp.name)
        tmp_path = tmp.name

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            # Конвертируем аудио в base64
            with open(tmp_path, "rb") as f:
                audio_base64 = base64.b64encode(f.read()).decode('utf-8')

            # Создаём сообщение с аудио
            prompt = "Распознай речь в этом аудио и выведи текст."
            human_msg = HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {
                        "type": "audio_url",
                        "audio_url": {"url": f"data:audio/ogg;base64,{audio_base64}"}
                    }
                ]
            )

            # Используем GigaChat с явной basic-авторизацией
            llm = GigaChat(
                credentials=CREDENTIALS,
                verify_ssl_certs=False,
                model="GigaChat-Pro",
                temperature=0.0,
                max_tokens=1024,
            )
            response = await llm.agenerate([[human_msg]])
            text = response.generations[0][0].text.strip()

        if not text:
            text = "[Не удалось распознать речь]"

        await message.reply(f"📝 Распознано: {text}")

        # Сохраняем транскрипцию в историю как сообщение от бота
        bot_me = await bot.me()
        await save_bot_message_to_history(
            chat_id=message.chat.id,
            text=f"🎤 Голосовое сообщение расшифровано: {text}",
            reply_to_message_id=message.message_id,
            bot_id=bot_me.id,
            bot_username=bot_me.username
        )

        logger.info(f"Голосовое от {message.from_user.id} транскрибировано: {text[:50]}...")
    except Exception as e:
        logger.error(f"Ошибка транскрипции через GigaChat: {e}", exc_info=True)
        await message.reply("❌ Не удалось распознать речь.")
    finally:
        os.unlink(tmp_path)
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
                executor = await gigachat_singleton.get_executor()

                token = current_chat_id_var.set(message.chat.id)
                try:
                    result = await executor.ainvoke({"input": message.text})
                    answer = result["output"]
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

async def on_shutdown():
    logger.info("👋 Бот останавливается...")
    await gigachat_singleton.close()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())