import re
import base64
import logging
from openai import AsyncOpenAI
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

_COLOR_RULE_LACQUER = (
    "КРИТИЧЕСКИ ВАЖНО: упаковка товара должна быть скопирована С РЕФЕРЕНСНОГО ФОТО ТОЧЬ-В-ТОЧЬ — "
    "форма, этикетка, пропорции без изменений. "
    "Лак прозрачный или тонированный — оттенок строго как на банке референса. "
    'Пиши: "упаковка скопирована точно с референса, лак прозрачный/тонированный как на банке"'
)

_COLOR_RULE_PRIMER = (
    "КРИТИЧЕСКИ ВАЖНО: упаковка товара должна быть скопирована С РЕФЕРЕНСНОГО ФОТО ТОЧЬ-В-ТОЧЬ — "
    "форма, этикетка, пропорции без изменений. "
    "Грунтовка белого или светло-серого цвета как на банке референса. "
    'Пиши: "упаковка скопирована точно с референса, грунтовка белого/серого цвета как на банке"'
)


def _build_system_prompt(paint_type: str) -> str:
    rules = {
        "walls": _COLOR_RULE_WALLS,
        "lacquer": _COLOR_RULE_LACQUER,
        "primer": _COLOR_RULE_PRIMER,
    }
    rule = rules.get(paint_type, _COLOR_RULE_FURNITURE)
    return _SYSTEM_PROMPT_BASE.format(color_rule=rule)


def _img_content(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


async def analyze_color_samples(color_image_bytes: list[bytes]) -> str:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    content: list = [_img_content(cb) for cb in color_image_bytes[:4]]
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
    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=200,
        messages=[{"role": "user", "content": content}],
    )
    return response.choices[0].message.content.strip()


async def generate_prompts(
    user_request: str,
    image_bytes: bytes | None = None,
    color_image_bytes: list[bytes] | None = None,
    paint_type: str = "furniture",
) -> list[str]:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    system = _build_system_prompt(paint_type)

    content: list = []

    # Фото упаковки НЕ передаём — GPT отказывается работать с брендированными изображениями.
    # Вся информация о товаре уже есть в текстовом запросе; фото нужно только PiAPI.

    if paint_type == "walls" and color_image_bytes:
        for cb in color_image_bytes[:4]:
            content.append(_img_content(cb))
        content.append({"type": "text", "text": "Образцы цвета и живые фото краски выше."})

    content.append({"type": "text", "text": user_request})

    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    )

    raw = response.choices[0].message.content.strip()
    logger.info("GPT generate_prompts response: %s", raw[:300])

    prompts = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r'^\d{1,2}[.)]\s+(.+)$', line)
        if m:
            prompts.append(m.group(1).strip())

    if len(prompts) < 4:
        raise ValueError(f"GPT вернул только {len(prompts)} промтов из 10. Ответ: {raw[:300]}")

    return prompts[:10]


_SLIDE_SYSTEM_PROMPT = """Ты — профессиональный дизайнер внутренних слайдов карточки товара для маркетплейсов (Ozon, Wildberries).

Задача: по запросу пользователя сгенерировать ровно 3 уникальных промта для нейросети Nano Banana Pro (Google Gemini image generator).

Правила:
1. Каждый промт — на РУССКОМ языке
2. Каждый промт описывает УНИКАЛЬНУЮ концепцию слайда: разная подача, композиция, акценты
3. КРИТИЧЕСКИ ВАЖНО: упаковка товара (банка/тара) копируется СТРОГО С ПРЕДОСТАВЛЕННОГО РЕНДЕРА ТОЧЬ-В-ТОЧЬ — точная форма, этикетка, цвет, пропорции БЕЗ КАКИХ-ЛИБО ИЗМЕНЕНИЙ. В каждом промте обязательно напиши: "банка скопирована точно с рендера без изменений"
4. Если предоставлен слайд конкурента — использовать его стиль и композицию как референс, не копируя бренд
5. Текстовые блоки описывай как: UI-плашка с текстом "текст"
6. Слайд должен быть информативным и продающим: показывать детали товара, процесс, результат или преимущества
7. Каждый промт заканчивай на: "Вертикальный формат 3:4, современный UX/UI дизайн, высококачественный внутренний слайд карточки товара для маркетплейса"

Верни ровно 3 промта в виде нумерованного списка — без JSON, без markdown, без пояснений:
1. промт один
2. промт два
3. промт три"""


async def generate_slide_prompts(
    user_request: str,
    paint_type: str = "furniture",
) -> list[str]:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _SLIDE_SYSTEM_PROMPT},
            {"role": "user", "content": user_request},
        ],
    )

    raw = response.choices[0].message.content.strip()
    logger.info("GPT generate_slide_prompts response: %s", raw[:300])

    prompts = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r'^\d{1,2}[.)]\s+(.+)$', line)
        if m:
            prompts.append(m.group(1).strip())

    if len(prompts) < 1:
        raise ValueError(f"GPT вернул только {len(prompts)} промтов из 3. Ответ: {raw[:300]}")

    return prompts[:3]
