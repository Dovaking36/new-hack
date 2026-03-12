import asyncio
from langchain_gigachat import GigaChat
import gigachat

CREDENTIALS = "MDE5Y2Q2OTYtMTk2ZC03YzVjLTgxZTQtOTk5NjhlNWRjYWFlOjFjZWU1YjI4LWRiYWUtNGIxMS05NGMyLTBlYmQ4NWEyMTVhYw=="

async def main():
    llm = GigaChat(credentials=CREDENTIALS, verify_ssl_certs=False)
    response = await llm.ainvoke("Привет, как дела?")
    print("Ответ модели:", response.content)

if __name__ == "__main__":
    print(gigachat.GigaChat.get_models())
    asyncio.run(main())