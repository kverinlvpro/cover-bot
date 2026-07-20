import re
import json
import base64
import logging
import httpx
import anthropic
import config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_BASE = """Ты — профессиональный дизайнер обложек для маркетплейсов (Ozon, Wildberries).

Задача: по запросу пользователя сгенерировать ровно 10 уникальных промтов для нейросети Nano Banana Pro (Google Gemini image generator).

Правила:
1. Каждый промт — на РУССКОМ языке
2. Каждый промт описывает УНИКАЛЬНУЮ концепцию: разная композиция, фон, настроение, ракурс
3. Все обязательные элементы из запроса пользователя присутствуют В КАЖДОМ промте
4. Текстовые оверлеи описывай как: UI-плашка с текстом "текст"
5. {color_rule}
6. Каждый промт заканчивай на: "Вертикальный формат 3:4, современный UX/UI дизайн, высококачественная коммерческая обложка для маркетплейса"

Структура каждого промта:
[Творческая концепция/сцена] + [Расположение товара с точной копией упаковки] + [Визуальные элементы] + [UI текстовые плашки] + [Стиль и формат]

Верни ровно 10 промтов в виде нумерованного списка — без JSON, без markdown, без пояснений:
1. промт один
2. промт два
...
10. промт десять"""

_COLOR_RULE_FURNITURE = (
    "КРИТИЧЕСКИ ВАЖНО: упаковка товара должна быть скопирована С РЕФЕРЕНСНОГО ФОТО ТОЧЬ-В-ТОЧЬ — "
    "форма, этикетка, пропорции без изменений. "
    "Оттенок краски внутри/снаружи банки совпадает с референсом. "
    'Пиши: "упаковка скопирована точно с референса, оттенок краски без изменений"'
)

_COLOR_RULE_WALLS = (
    "ВАЖНО: это краска для стен — банка на обложке БЕЛАЯ (белая непрозрачная тара, форма и этикетка берётся с референсного фото). "
    "Цвет и оттенок краски определяется ТОЛЬКО по предоставленным образцам цвета и живым фотографиям. "
    'Пиши: "банка белая как на референсе, цвет краски взят из образца цвета и живых фото"'
)


def _build_system_prompt(paint_type: str) -> str:
    rule = _COLOR_RULE_WALLS if paint_type == "walls" else _COLOR_RULE_FURNITURE
    return _SYSTEM_PROMPT_BASE.format(color_rule=rule)


_CARD_ANALYSIS_PROMPT = (
    "Проанализируй содержимое страницы товара с маркетплейса и верни ТОЛЬКО JSON без markdown:\n"
    '{"name": "название товара", '
    '"volume": "объём/вес/расход (2.5л, 5кг, 9м²/л) или null", '
    '"paint_type": "furniture или walls", '
    '"utps": ["УТП 1", "УТП 2", "УТП 3", "УТП 4", "УТП 5", "УТП 6"]}\n'
    "paint_type: walls — краска для стен/потолка/фасада/интерьера; furniture — для мебели/дерева/металла.\n"
    "volume: ищи в характеристиках (Объём, Вес, Расход), в скобках в названии, в описании.\n"
    "utps — 6-10 коротких торговых преимуществ (3-5 слов каждое)."
)

_BROWSER_CONFIGS = [
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Google Chrome";v="125", "Chromium";v="125", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?1",
        "Sec-CH-UA-Platform": '"Android"',
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Google Chrome";v="125", "Chromium";v="125", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    },
]


def _extract_page_content(html: str) -> str:
    jsonld = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        html, re.IGNORECASE,
    )
    jsonld_text = "\n".join(jsonld[:3])[:3000]
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    prefix = f"STRUCTURED DATA:\n{jsonld_text}\n\nPAGE TEXT:\n" if jsonld_text else ""
    return prefix + text[:20000]


_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_WARMUP_TIMEOUT = httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=4.0)


async def _fetch_page(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}/"

    for cfg in _BROWSER_CONFIGS:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as http:
                try:
                    await http.get(base_url, headers=cfg, timeout=_WARMUP_TIMEOUT)
                except Exception:
                    pass
                r = await http.get(url, headers={**cfg, "Referer": base_url})
                logger.info("_fetch_page status=%d ua=%s", r.status_code, cfg["User-Agent"][:40])
                if r.status_code == 200 and len(r.text) > 500:
                    return _extract_page_content(r.text)
        except Exception as e:
            logger.warning("_fetch_page exception: %s", e)
    return ""


async def analyze_card(url: str) -> dict:
    page_content = await _fetch_page(url)

    if not page_content:
        raise ValueError(
            "Страница недоступна — маркетплейс заблокировал запрос.\n"
            "Попробуйте другую ссылку или используйте режим «Гибкая настройка»."
        )

    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    prompt = f"URL: {url}\n\nСОДЕРЖИМОЕ СТРАНИЦЫ:\n{page_content}\n\n{_CARD_ANALYSIS_PROMPT}"

    try:
        response = await client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise ValueError(f"Ошибка Claude API: {e}")

    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    logger.info("analyze_card response: %s", text[:400])

    if "{" in text:
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
        s = clean.find("{")
        e = clean.rfind("}") + 1
        if s >= 0 and e > s:
            try:
                result = json.loads(clean[s:e])
                if "name" in result and "utps" in result:
                    if "paint_type" not in result:
                        result["paint_type"] = "furniture"
                    return result
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Не удалось извлечь JSON из ответа Claude:\n{text[:300]}")


async def analyze_color_samples(color_image_bytes: list[bytes]) -> str:
    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    content: list = []
    for cb in color_image_bytes[:4]:
        b64 = base64.standard_b64encode(cb).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({
        "type": "text",
        "text": (
            "Внимательно изучи образцы цвета и живые фото. "
            "Дай точное описание цвета краски для использования в промте нейросети — "
            "ответь ОДНИМ предложением на русском. "
            "Укажи: точный оттенок, тон (тёплый/холодный/нейтральный), насыщенность, "
            "ближайший аналог из понятных цветов. "
            "Пример ответа: «Тёплый светло-бежевый оттенок с нотками слоновой кости, "
            "почти белый, очень светлый, матовый, warm white.»"
        ),
    })
    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=200,
        messages=[{"role": "user", "content": content}],
    )
    return next(b.text for b in response.content if hasattr(b, "text")).strip()


async def generate_prompts(
    user_request: str,
    image_bytes: bytes | None = None,
    color_image_bytes: list[bytes] | None = None,
    paint_type: str = "furniture",
) -> list[str]:
    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    system = _build_system_prompt(paint_type)

    content: list = []

    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
        content.append({"type": "text", "text": "Референсное фото упаковки товара выше."})

    if paint_type == "walls" and color_image_bytes:
        for i, cb in enumerate(color_image_bytes[:4]):
            b64 = base64.standard_b64encode(cb).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })
        content.append({"type": "text", "text": "Образцы цвета и живые фото краски выше."})

    content.append({"type": "text", "text": user_request})

    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}]
    )

    raw = next(block.text for block in response.content if hasattr(block, "text")).strip()

    prompts = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r'^\d{1,2}[.)]\s+(.+)$', line)
        if m:
            prompts.append(m.group(1).strip())

    if len(prompts) < 8:
        raise ValueError(f"Claude вернул только {len(prompts)} промтов из 10. Ответ: {raw[:300]}")

    return prompts[:10]
