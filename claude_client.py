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


CARD_ANALYSIS_SYSTEM = (
    "Ты аналитик товарных карточек маркетплейсов. "
    "Используй web_fetch чтобы загрузить страницу товара. "
    "После загрузки верни ТОЛЬКО JSON без markdown и пояснений:\n"
    '{"name": "название товара из h1", '
    '"volume": "объём/вес если указан в названии (например: 360г, 1л), иначе null", '
    '"paint_type": "furniture" или "walls", '
    '"utps": ["УТП 1", "УТП 2", "УТП 3", "УТП 4", "УТП 5", "УТП 6"]}\n'
    "paint_type: walls — если краска для стен/потолка/фасада; furniture — для мебели/дерева/металла.\n"
    "utps — 6-10 коротких уникальных торговых преимуществ (3-5 слов каждое) из описания товара."
)

_FETCH_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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


async def _fetch_page(url: str) -> str:
    for ua in _FETCH_AGENTS:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20) as http:
                r = await http.get(url, headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                })
                logger.info("_fetch_page status=%d url=%s", r.status_code, url[:80])
                if r.status_code == 200:
                    return _extract_page_content(r.text)
        except Exception as e:
            logger.warning("_fetch_page exception: %s", e)
    return ""


async def analyze_card(url: str) -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    tools = [{"type": "web_fetch_20260209", "name": "web_fetch"}]
    messages: list = [{"role": "user", "content": f"Проанализируй карточку товара: {url}"}]
    last_text = ""

    for turn in range(5):
        try:
            response = await client.messages.create(
                model="claude-sonnet-5",
                max_tokens=2048,
                tools=tools,
                system=CARD_ANALYSIS_SYSTEM,
                messages=messages,
            )
        except Exception as e:
            logger.error("analyze_card API error turn=%d: %s", turn, e)
            raise ValueError(f"Ошибка API (шаг {turn}): {e}")

        logger.info(
            "analyze_card turn=%d stop=%s blocks=%s",
            turn, response.stop_reason,
            [type(b).__name__ for b in response.content],
        )

        text = next((b.text for b in response.content if hasattr(b, "text")), "")
        if text:
            last_text = text
            logger.info("analyze_card text=%s", text[:400])

        if text and "{" in text:
            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
            s = clean.find("{")
            e = clean.rfind("}") + 1
            if s >= 0 and e > s:
                try:
                    result = json.loads(clean[s:e])
                    if "name" in result and "utps" in result:
                        # Default paint_type to furniture if not detected
                        if "paint_type" not in result:
                            result["paint_type"] = "furniture"
                        return result
                except json.JSONDecodeError:
                    pass

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "web_fetch":
                    fetch_url = block.input.get("url", url)
                    logger.info("analyze_card tool web_fetch url=%s", fetch_url)
                    page = await _fetch_page(fetch_url)
                    if not page:
                        page = "Страница недоступна (HTTP 403). Данных нет."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": page,
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        break

    raise ValueError(
        f"Не удалось извлечь данные карточки.\n"
        f"Последний ответ Claude: {last_text[:300] or '(пусто)'}"
    )


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
