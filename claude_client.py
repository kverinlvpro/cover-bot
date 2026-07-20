import re
import json
import base64
import httpx
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


def _extract_page_content(html: str) -> str:
    # Extract JSON-LD structured data (has clean product info)
    jsonld = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        html, re.IGNORECASE
    )
    jsonld_text = "\n".join(jsonld[:3])[:4000]

    # Strip HTML to plain text
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    result = ""
    if jsonld_text:
        result = f"STRUCTURED DATA:\n{jsonld_text}\n\nPAGE TEXT:\n"
    return result + text[:25000]


async def analyze_card(url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
        r = await http.get(url, headers=headers)
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        content = _extract_page_content(r.text)

    client = anthropic.AsyncAnthropic(api_key=config.CLAUDE_API_KEY)
    prompt = (
        f"Проанализируй контент страницы товара с маркетплейса и извлеки информацию.\n\n"
        f"КОНТЕНТ СТРАНИЦЫ:\n{content}\n\n"
        f"Верни ТОЛЬКО JSON без пояснений и markdown:\n"
        f'{{"name": "полное название товара из h1", '
        f'"volume": "объём/вес если указан в названии (например: 360г, 1л), иначе null", '
        f'"utps": ["УТП 1", "УТП 2", "УТП 3", "УТП 4", "УТП 5", "УТП 6"]}}\n\n'
        f"Для utps — 6-10 коротких уникальных торговых преимуществ (3-5 слов каждое) "
        f"на основе описания и характеристик товара."
    )

    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = next(block.text for block in response.content if hasattr(block, "text")).strip()
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    start = clean.find("{")
    end = clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"Claude не вернул JSON: {raw[:200]}")

    result = json.loads(clean[start:end])
    if "name" not in result or "utps" not in result:
        raise ValueError(f"Неверный формат ответа Claude")
    return result


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
