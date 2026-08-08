import asyncio
import base64
import io
import logging

from openai import AsyncOpenAI
import config

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(2)
last_error: str = ""

_SIZE_MAP = {
    "3:4":  "1536x2048",
    "9:16": "1152x2048",
    "16:9": "2048x1152",
    "1:1":  "2048x2048",
}


async def generate_image(
    prompt: str,
    image_bytes_list: list[bytes] | None = None,
    aspect_ratio: str = "3:4",
) -> bytes | None:
    global last_error
    size = _SIZE_MAP.get(aspect_ratio, "1536x2048")
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

    async with _sem:
        try:
            if image_bytes_list:
                images = [io.BytesIO(b) for b in image_bytes_list[:4]]
                response = await client.images.edit(
                    model="gpt-image-2",
                    image=images if len(images) > 1 else images[0],
                    prompt=prompt,
                    size=size,
                    quality="high",
                    n=1,
                )
            else:
                response = await client.images.generate(
                    model="gpt-image-2",
                    prompt=prompt,
                    size=size,
                    quality="high",
                    n=1,
                )
            b64 = response.data[0].b64_json
            if not b64:
                last_error = "empty b64_json in response"
                logger.error("gpt_image_client: %s", last_error)
                return None
            last_error = ""
            return base64.b64decode(b64)
        except Exception as e:
            last_error = str(e)
            logger.error("gpt_image_client.generate_image error: %s", e)
            return None
