import asyncio
import logging
import httpx
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.piapi.ai/api/v1/task"


def _headers() -> dict:
    return {"X-API-Key": config.PIAPI_KEY, "Content-Type": "application/json"}


async def _submit(prompt: str, image_urls: list[str] | None, aspect_ratio: str | None = None) -> tuple[str | None, str | None]:
    payload: dict = {
        "model": "gemini",
        "task_type": config.TASK_TYPE,
        "input": {
            "prompt": prompt,
            "output_format": "jpg",
            "aspect_ratio": aspect_ratio or config.ASPECT_RATIO,
            "resolution": config.RESOLUTION,
        }
    }
    if image_urls:
        payload["input"]["image_urls"] = image_urls

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(BASE_URL, json=payload, headers=_headers())
            data = r.json()

        code = data.get("code")
        logger.info("PiAPI submit http=%s code=%s task_type=%s", r.status_code, code, config.TASK_TYPE)
        if code != 200:
            err = data.get("message") or data.get("error") or str(data)[:300]
            logger.error("PiAPI submit FULL response: %s", data)
            return None, err

        task_id = data["data"]["task_id"]
        logger.info("PiAPI submit OK task_id=%s", task_id)
        return task_id, None
    except Exception as e:
        logger.exception("PiAPI submit exception: %s", e)
        return None, str(e)


async def _poll(task_id: str, timeout: int = 180, interval: int = 5) -> str | None:
    deadline = asyncio.get_event_loop().time() + timeout
    checks = 0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)
        checks += 1
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{BASE_URL}/{task_id}", headers=_headers())
                data = r.json()

            task = data.get("data", {})
            status = task.get("status", "")
            logger.info("PiAPI poll #%d task_id=%s status=%s", checks, task_id, status)

            if status == "completed":
                out = task.get("output", {})
                urls = out.get("image_urls") or []
                url = urls[0] if urls else out.get("image_url")
                logger.info("PiAPI completed task_id=%s url_ok=%s", task_id, bool(url))
                return url

            if status in ("failed", "error", "cancelled"):
                logger.error("PiAPI task FAILED task_id=%s full=%s", task_id, task)
                return None

        except Exception as e:
            logger.exception("PiAPI poll exception check=%d: %s", checks, e)
            continue

    logger.error("PiAPI TIMEOUT after %ds task_id=%s checks=%d", timeout, task_id, checks)
    return None


# Лимит одновременно активных задач в PiAPI (submit + ожидание результата)
_sem = asyncio.Semaphore(3)
last_error: str = ""   # last human-readable error, readable from run_pipeline


async def generate_image(prompt: str, image_urls: list[str] | None = None, aspect_ratio: str | None = None) -> str | None:
    global last_error
    async with _sem:
        for attempt in range(3):
            logger.info("PiAPI generate attempt=%d image_urls_count=%d", attempt + 1, len(image_urls or []))
            task_id, submit_err = await _submit(prompt, image_urls, aspect_ratio)
            if not task_id:
                last_error = submit_err or "submit failed"
                wait = (attempt + 1) * 5
                logger.warning("PiAPI submit failed attempt=%d error=%s retry in %ds", attempt + 1, last_error, wait)
                await asyncio.sleep(wait)
                continue

            url = await _poll(task_id)
            if url:
                last_error = ""
                return url

            last_error = f"task {task_id} failed/timed out"
            logger.warning("PiAPI poll returned None attempt=%d task_id=%s", attempt + 1, task_id)
            await asyncio.sleep((attempt + 1) * 5)

        logger.error("PiAPI all 3 attempts failed for prompt=%.80s", prompt)
        return None
