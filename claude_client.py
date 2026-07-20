import json
import base64
import traceback
import anthropic
import config

SYSTEM_PROMPT = """You are a professional marketplace cover designer (Ozon, Wildberries).

Task: Based on the user's request, generate exactly 10 unique prompts for the Nano Banana Pro neural network (Google Gemini image generator).

Rules:
1. Each prompt MUST be in ENGLISH
2. Each prompt describes a UNIQUE concept: different composition, background, mood, angle
3. All required elements from the user's request are present in EVERY prompt
4. Text overlays are described as: UI badge with text "text"
5. CRITICAL — reference image: if a reference image is provided, the product packaging must be copied EXACTLY from the reference without any changes to shape, label, color, or proportions. Write in the prompt: "product packaging copied exactly from the reference image, shape and label unchanged"
6. End each prompt with: "Vertical 3:4 format, modern UX/UI design, high-quality commercial marketplace cover"

For each prompt also provide a SHORT Russian description of the concept (1-2 sentences) so the user understands the idea.

Return ONLY a JSON array of 10 objects, no explanations or markdown:
[{"en": "english prompt here", "ru": "русское описание концепции"}, ...]"""


async def generate_prompts(user_request: str, image_bytes: bytes | None = None) -> list[dict]:
    try:
        return await _generate_prompts_inner(user_request, image_bytes)
    except Exception:
        raise ValueError(traceback.format_exc())


async def _generate_prompts_inner(user_request: str, image_bytes: bytes | None = None) -> list[dict]:
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
            "text": f"Reference product photo above.\n\n{user_request}"
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
        raise ValueError(f"Claude did not return a JSON array: {raw[:300]}")

    prompts = json.loads(raw[start:end])
    if not isinstance(prompts, list) or len(prompts) != 10:
        raise ValueError(f"Expected 10 prompts, got {len(prompts)}")

    return prompts
