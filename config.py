import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
PIAPI_KEY: str = os.environ["PIAPI_KEY"]

ASPECT_RATIO: str = os.getenv("ASPECT_RATIO", "1:1")
RESOLUTION: str = os.getenv("RESOLUTION", "1K")
TASK_TYPE: str = os.getenv("TASK_TYPE", "nano-banana-2")
NUM_IMAGES: int = int(os.getenv("NUM_IMAGES", "10"))

# Доступ: telegram user_id администратора и разрешённых пользователей
ADMIN_USER_ID: int = int(os.getenv("ADMIN_USER_ID", "0"))
# Дополнительные разрешённые ID через запятую (постоянные, из env)
ALLOWED_USER_IDS: list[int] = [
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
]
