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
5. КРИТИЧЕСКИ ВАЖНО: если есть референсное изображение — упаковка товара должна быть скопирована С РЕФЕРЕНСА ТОЧЬ-В-ТОЧЬ, без каких-либо изменений формы, этикетки, цвета, пропорций. Пиши: "упаковка товара скопирована точно с референсного изображения, форма и этикетка без изменений"
6. Каждый промт заканчивай на: "Вертикальный формат 3:4, современный UX/UI дизайн, высококачественная коммерческая обложка для маркетплейса"

Структура каждого промта:
[Творческая концепция/сцена] + [Расположение товара с точной копией упаковки] + [Визуальные элементы] + [UI текстовые плашки] + [Стиль и формат]

Верни ТОЛЬКО JSON-массив из 10 строк, без пояснений и markdown:
["промт 1", "промт 2", ..., "промт 10"]"""


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

    prompts = json.loads(raw[start:end])
    if not isinstance(prompts, list) or len(prompts) != 10:
        raise ValueError(f"Ожидалось 10 промтов, получено {len(prompts)}")

    return prompts
