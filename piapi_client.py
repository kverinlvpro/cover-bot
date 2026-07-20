import asyncio
import logging
import httpx
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.piapi.ai/api/v1/task"


def _headers() -> dict:
    return {"X-API-Key": config.PIAPI_KEY, "Content-Type": "application/json"}


async def _submit(prompt: str, image_urls: list[str] | None) -> str | None:
    payload: dict = {
        "model": "gemini",
        "task_type": config.TASK_TYPE,
        "input": {
            "prompt": prompt,
            "output_format": "jpg",
            "aspect_ratio": config.ASPECT_RATIO,
            "resolution": config.RESOLUTION,
        }
    }
    if image_urls:
        payload["input"]["image_urls"] = image_urls

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(BASE_URL, json=payload, headers=_headers())
            data = r.json()

        logger.info("PiAPI submit status=%s code=%s", r.status_code, data.get("code"))
        if data.get("code") != 200:
            logger.error("PiAPI submit error: %s", data)
            return None

        return data["data"]["task_id"]
    except Exception as e:
        logger.exception("PiAPI submit exception: %s", e)
        return None


async def _poll(task_id: str, timeout: int = 180, interval: int = 5) -> str | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE_URL}/{task_id}", headers=_headers())
                data = r.json()

            task = data.get("data", {})
            status = task.get("status", "")
            logger.info("PiAPI poll task_id=%s status=%s", task_id, status)

            if status == "completed":
                out = task.get("output", {})
                urls = out.get("image_urls") or []
                return urls[0] if urls else out.get("image_url")

            if status in ("failed", "error", "cancelled"):
                logger.error("PiAPI task failed: %s", task)
                return None

        except Exception as e:
            logger.exception("PiAPI poll exception: %s", e)
            continue

    logger.error("PiAPI poll timeout for task_id=%s", task_id)
    return None


async def generate_image(prompt: str, image_urls: list[str] | None = None) -> str | None:
    task_id = await _submit(prompt, image_urls)
    if not task_id:
        await asyncio.sleep(3)
        task_id = await _submit(prompt, image_urls)
        if not task_id:
            return None
    return await _poll(task_id)
