import asyncio
import base64
import io
import logging

from openai import AsyncOpenAI
import config

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(2)  # gpt-image-1 rate limit is lower than piapi

_SIZE_MAP = {
    "3:4":  "1024x1536",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
    "1:1":  "1024x1024",
}


async def generate_image(
    prompt: str,
    image_bytes_list: list[bytes] | None = None,
    aspect_ratio: str = "3:4",
) -> bytes | None:
    """Generate an image with gpt-image-1. Returns raw JPEG/PNG bytes, or None on failure."""
    size = _SIZE_MAP.get(aspect_ratio, "1024x1536")
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

    async with _sem:
        try:
            if image_bytes_list:
                images = [io.BytesIO(b) for b in image_bytes_list[:4]]
                response = await client.images.edit(
                    model="gpt-image-1",
                    image=images if len(images) > 1 else images[0],
                    prompt=prompt,
                    size=size,
                    quality="high",
                    n=1,
                )
            else:
                response = await client.images.generate(
                    model="gpt-image-1",
                    prompt=prompt,
                    size=size,
                    quality="high",
                    n=1,
                )
            b64 = response.data[0].b64_json
            if not b64:
                logger.error("gpt_image_client: empty b64_json in response")
                return None
            return base64.b64decode(b64)
        except Exception as e:
            logger.error("gpt_image_client.generate_image error: %s", e)
            return None
