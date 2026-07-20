import re
import json
import base64
import anthropic
import config

SYSTEM_PROMPT = """Ты — профессиональный дизайнер обложек для маркетплейсов (Ozon, Wildberries).

Задача: по запросу пользователя сгенерировать ровно 10 уникальных промтов для нейросети Nano Banana Pro (Google Gemini image generator).

Правила:
1. Каждый промт — на РУССКОМ языке
2. Каждый промт описывает УНИКАЛЬНУЮ концепцию: разная композиция, фон, настроение, ракурс
3. Все обязательные элементы из запроса пользователя присутствуют В КАЖДОМ промте
4. Текстовые оверлеи описывай как: UI-плашка с текстом "текст"
5. КРИТИЧЕСКИ ВАЖНО: если есть референсное изображение — упаковка товара должна быть скопирована С РЕФЕРЕНСА ТОЧЬ-В-ТОЧЬ, без каких-либо изменений формы, этикетки, цвета, пропорций. Оттенок краски/продукта внутри или снаружи банки должен точно совпадать с оттенком на референсе. Пиши: "упаковка товара скопирована точно с референсного изображения, форма, этикетка и оттенок краски без изменений"
6. Каждый промт заканчивай на: "Вертикальный формат 3:4, современный UX/UI дизайн, высококачественная коммерческая обложка для маркетплейса"

Структура каждого промта:
[Творческая концепция/сцена] + [Расположение товара с точной копией упаковки] + [Визуальные элементы] + [UI текстовые плашки] + [Стиль и формат]

Верни ТОЛЬКО JSON-массив из 10 строк, без пояснений и markdown:
["промт 1", "промт 2", ..., "промт 10"]"""


CARD_ANALYSIS_SYSTEM = (
    "Ты аналитик товарных карточек маркетплейсов. "
    "Используй web_fetch чтобы загрузить страницу товара. "
    "После загрузки верни ТОЛЬКО JSON без markdown и пояснений:\n"
    '{"name": "название товара из h1", '
    '"volume": "объём/вес если указан в названии (например: 360г, 1л), иначе null", '
    '"utps": ["УТП 1", "УТП 2", "УТП 3", "УТП 4", "УТП 5", "УТП 6"]}\n'
    "utps — 6-10 коротких уникальных торговых преимуществ (3-5 слов каждое) "
    "из описания и характеристик товара."
)


async def analyze_card(url: str) -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    tools = [{"type": "web_fetch_20260209", "name": "web_fetch"}]
    messages = [{"role": "user", "content": f"Проанализируй карточку товара: {url}"}]

    # Server-side tool loop: Anthropic executes web_fetch on their infrastructure
    for _ in range(5):
        response = await client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            tools=tools,
            system=CARD_ANALYSIS_SYSTEM,
            messages=messages,
        )

        text = next((b.text for b in response.content if hasattr(b, "text")), "")

        if text and "{" in text:
            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
            start = clean.find("{")
            end = clean.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(clean[start:end])
                if "name" in result and "utps" in result:
                    return result

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            continue

        break

    raise ValueError("Claude не смог проанализировать карточку")


async def generate_prompts(user_request: str, image_bytes: bytes | None = None) -> list[str]:
    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)

    content: list = []

    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            }
        })
        content.append({
            "type": "text",
            "text": f"Референсное фото товара выше.\n\n{user_request}"
        })
    else:
        content.append({"type": "text", "text": user_request})

    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}]
    )

    raw = next(block.text for block in response.content if hasattr(block, "text")).strip()

    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"Claude не вернул JSON-массив: {raw[:300]}")

    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw[start:end])
    prompts = json.loads(json_str)
    if not isinstance(prompts, list) or len(prompts) != 10:
        raise ValueError(f"Ожидалось 10 промтов, получено {len(prompts)}")

    return prompts
