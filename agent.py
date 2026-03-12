import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain.tools import tool
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_gigachat import GigaChat

from system_prompts import system_prompt_v1

# ================== НАСТРОЙКИ ==================
CREDENTIALS = "MDE5Y2Q2OTYtMTk2ZC03YzVjLTgxZTQtOTk5NjhlNWRjYWFlOjFjZWU1YjI4LWRiYWUtNGIxMS05NGMyLTBlYmQ4NWEyMTVhYw=="
HISTORY_DIR = "chat_history"

# ================== ИНСТРУМЕНТЫ ==================

@tool
async def summator(a: float, b: float) -> str:
    """Складывает два числа и возвращает результат."""
    result = a + b
    return json.dumps({"result": result, "expression": f"{a} + {b}"}, ensure_ascii=False)

@tool
async def get_chat_id(run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
    """Возвращает ID текущего чата Telegram из контекста."""
    if run_manager and run_manager.metadata:
        chat_id = run_manager.metadata.get("chat_id")
        if chat_id:
            return json.dumps({"chat_id": chat_id}, ensure_ascii=False)
    return json.dumps({"error": "Не удалось определить chat_id"}, ensure_ascii=False)

def create_notification_tool(bot):
    @tool
    async def send_notification(chat_id: int, text: str) -> str:
        """Отправляет уведомление в указанный чат Telegram. Используй, когда нужно оповестить пользователей о чём-то важном."""
        if not chat_id or not text:
            return json.dumps({"error": "Не указаны chat_id или text"}, ensure_ascii=False)
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
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        """Читает историю сообщений из указанного чата. Если chat_id не указан, используется текущий чат из контекста."""
        # Если chat_id не передан, пытаемся получить из контекста
        if chat_id is None:
            if run_manager and run_manager.metadata:
                chat_id = run_manager.metadata.get("chat_id")
            if chat_id is None:
                return json.dumps({"error": "Не указан chat_id и не удалось определить из контекста"}, ensure_ascii=False)

        # Остальной код без изменений
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

# ================== СОЗДАНИЕ АГЕНТА ==================
def create_agent(bot):
    tools = [
        summator,
        get_chat_id,
        create_notification_tool(bot),
        create_read_history_tool(HISTORY_DIR),
    ]

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt_v1),
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