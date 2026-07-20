import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY: str = os.environ["CLAUDE_API_KEY"]
PIAPI_KEY: str = os.environ["PIAPI_KEY"]

ASPECT_RATIO: str = os.getenv("ASPECT_RATIO", "1:1")
RESOLUTION: str = os.getenv("RESOLUTION", "1K")
TASK_TYPE: str = os.getenv("TASK_TYPE", "nano-banana-2")
NUM_IMAGES: int = int(os.getenv("NUM_IMAGES", "10"))
