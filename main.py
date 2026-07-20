import asyncio
import logging
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
import claude_client
import piapi_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

bot = Bot(token=config.TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Хранилище: image_id -> {"prompt": str, "url": str}
_image_store: dict[str, dict] = {}


class CoverForm(StatesGroup):
    product_name = State()
    volume = State()
    headline = State()
    subtitle = State()
    badges = State()
    design_request = State()
    photos = State()


class FixForm(StatesGroup):
    awaiting_correction = State()


class MultiplyCallback(CallbackData, prefix="mul"):
    image_id: str


class FixCallback(CallbackData, prefix="fix"):
    image_id: str


# --- Клавиатуры ---

def _kb(*labels: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=l) for l in labels]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


SKIP_KB = _kb("Пропустить")
DONE_SKIP_KB = _kb("Готово", "Пропустить")


def _image_kb(image_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔁 Размножить идею",
            callback_data=MultiplyCallback(image_id=image_id).pack(),
        )],
        [InlineKeyboardButton(
            text="✏️ Исправить фотографию",
            callback_data=FixCallback(image_id=image_id).pack(),
        )],
    ])


# --- /start и /cancel ---

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Cover Bot — генератор обложек для маркетплейсов</b>\n\n"
        "Отвечайте на вопросы по очереди — в конце получите 10 уникальных обложек.\n\n"
        "Введите <b>название товара</b>:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(CoverForm.product_name)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Отменено. Нажмите /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --- Шаги формы ---

@dp.message(CoverForm.product_name, F.text)
async def step_product_name(message: Message, state: FSMContext):
    await state.update_data(product_name=message.text.strip())
    await message.answer(
        "Введите <b>объём товара</b> (например: 360г, 1л, 500мл):",
        parse_mode="HTML",
    )
    await state.set_state(CoverForm.volume)


@dp.message(CoverForm.volume, F.text)
async def step_volume(message: Message, state: FSMContext):
    await state.update_data(volume=message.text.strip())
    await message.answer(
        "Введите <b>заголовок</b> — главный текст на обложке:",
        parse_mode="HTML",
    )
    await state.set_state(CoverForm.headline)


@dp.message(CoverForm.headline, F.text)
async def step_headline(message: Message, state: FSMContext):
    await state.update_data(headline=message.text.strip())
    await message.answer(
        "Введите <b>подзаголовок</b>:",
        parse_mode="HTML",
    )
    await state.set_state(CoverForm.subtitle)


@dp.message(CoverForm.subtitle, F.text)
async def step_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await message.answer(
        "Введите <b>плашки свойств</b> — преимущества через запятую:\n"
        "<i>Пример: улучшает сцепление, для любых поверхностей, быстро сохнет</i>",
        parse_mode="HTML",
    )
    await state.set_state(CoverForm.badges)


@dp.message(CoverForm.badges, F.text)
async def step_badges(message: Message, state: FSMContext):
    await state.update_data(badges=message.text.strip())
    await message.answer(
        "Введите <b>дизайнерский запрос</b> — особая деталь на каждой обложке:\n"
        "<i>Пример: малярная кисть, фото ДО/ПОСЛЕ, живые цветы</i>\n\n"
        "Или нажмите «Пропустить»",
        parse_mode="HTML",
        reply_markup=SKIP_KB,
    )
    await state.set_state(CoverForm.design_request)


@dp.message(CoverForm.design_request, F.text)
async def step_design_request(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(
        design_request=None if text == "Пропустить" else text,
        photo_ids=[],
    )
    await message.answer(
        "Прикрепите <b>фото-референсы товара</b> (до 4 штук).\n"
        "Можно отправить альбом сразу или по одному фото.\n"
        "Когда все загружены — нажмите «Готово».\n"
        "Если референсы не нужны — «Пропустить».",
        parse_mode="HTML",
        reply_markup=DONE_SKIP_KB,
    )
    await state.set_state(CoverForm.photos)


# --- Приём фото на шаге photos ---

_media_groups: dict[str, list[Message]] = {}
_media_group_tasks: dict[str, asyncio.Task] = {}


async def _flush_media_group(group_id: str, state: FSMContext, reply_to: Message):
    await asyncio.sleep(0.6)
    msgs = _media_groups.pop(group_id, [])
    _media_group_tasks.pop(group_id, None)
    if not msgs:
        return
    msgs.sort(key=lambda m: m.message_id)
    data = await state.get_data()
    existing: list = data.get("photo_ids", [])
    new_ids = [m.photo[-1].file_id for m in msgs if m.photo]
    combined = (existing + new_ids)[:4]
    await state.update_data(photo_ids=combined)
    await reply_to.answer(
        f"Получено {len(combined)} фото. Отправьте ещё или нажмите «Готово».",
        reply_markup=DONE_SKIP_KB,
    )


@dp.message(CoverForm.photos, F.media_group_id & F.photo)
async def photos_media_group(message: Message, state: FSMContext):
    gid = message.media_group_id
    _media_groups.setdefault(gid, []).append(message)
    if gid in _media_group_tasks:
        _media_group_tasks[gid].cancel()
    _media_group_tasks[gid] = asyncio.create_task(
        _flush_media_group(gid, state, message)
    )


@dp.message(CoverForm.photos, F.photo & ~F.media_group_id)
async def photos_single(message: Message, state: FSMContext):
    data = await state.get_data()
    existing: list = data.get("photo_ids", [])
    combined = (existing + [message.photo[-1].file_id])[:4]
    await state.update_data(photo_ids=combined)
    await message.answer(
        f"Фото {len(combined)}/4. Отправьте ещё или нажмите «Готово».",
        reply_markup=DONE_SKIP_KB,
    )


@dp.message(CoverForm.photos, F.text.in_({"Готово", "Пропустить"}))
async def photos_done(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("Принято! Запускаю генерацию…", reply_markup=ReplyKeyboardRemove())
    await run_pipeline(message, data)


# --- Утилиты ---

def _build_request(data: dict) -> str:
    product = data["product_name"]
    volume = data["volume"]
    headline = data["headline"]
    subtitle = data["subtitle"]
    badges = data["badges"]
    design = data.get("design_request")
    has_photos = bool(data.get("photo_ids"))

    design_part = (
        f" В каждой идее обязательно должен присутствовать {design}." if design else ""
    )

    points = [
        f'1) Нужно сделать дополнительные плашки с преимуществами: "{badges}".',
        f"2) Плашку с объёмом {volume}.",
        f"3) Заголовок: {headline} и подзаголовок: {subtitle}.",
    ]
    if has_photos:
        points.append(
            "4) Товар (упаковку/банку) взять СТРОГО с референсного изображения "
            "без каких-либо изменений формы, этикетки и цвета."
        )
    points.append(f"{len(points) + 1}) Дизайн должен быть выполнен в современном UX/UI стиле.")

    return (
        f'Мне нужно сделать 10 креативных нетипичных идей для продающей обложки карточки товара "{product}".{design_part} '
        f"Каждую идею нужно расписать как тз промт для Nano Banana Pro. "
        f"В каждое тз нужно добавить эти пункты:\n"
        + "\n".join(points)
    )


async def _tg_url(file_id: str) -> str | None:
    try:
        file = await bot.get_file(file_id)
        return f"https://api.telegram.org/file/bot{config.TELEGRAM_TOKEN}/{file.file_path}"
    except Exception:
        return None


async def _send_image(target: Message, url: str, prompt: str, label: str):
    image_id = uuid.uuid4().hex[:10]
    _image_store[image_id] = {"prompt": prompt, "url": url}
    caption = f"{label}\n\n<i>{prompt[:800]}</i>"
    try:
        await target.answer_photo(
            photo=url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_image_kb(image_id),
        )
    except Exception:
        await target.answer(f"{label}: фото готово, но не удалось отправить.")


# --- Основной пайплайн ---

async def run_pipeline(message: Message, data: dict):
    user_request = _build_request(data)
    photo_ids: list[str] = data.get("photo_ids", [])

    status = await message.answer("Генерирую промты через Claude…")

    image_bytes: bytes | None = None
    if photo_ids:
        try:
            file = await bot.get_file(photo_ids[0])
            buf = await bot.download_file(file.file_path)
            image_bytes = buf.read()
        except Exception:
            pass

    try:
        prompts = await claude_client.generate_prompts(user_request, image_bytes)
    except Exception as e:
        await status.edit_text(f"Ошибка генерации промтов: {e}")
        return

    await status.edit_text(
        "10 промтов готовы! Отправляю в Nano Banana Pro…\n"
        "Обычно занимает 1–2 минуты."
    )

    ref_urls: list[str] = []
    for fid in photo_ids[:4]:
        url = await _tg_url(fid)
        if url:
            ref_urls.append(url)

    done = {"n": 0, "ok": 0}

    async def gen_and_send(idx: int, prompt: str):
        url = await piapi_client.generate_image(prompt, ref_urls or None)
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(message, url, prompt, f"Вариант {idx}/10")
        else:
            await message.answer(f"Вариант {idx}: генерация не удалась.")
        try:
            await status.edit_text(f"Обработано {done['n']}/10 | Готово: {done['ok']}")
        except Exception:
            pass

    await asyncio.gather(*[gen_and_send(i + 1, p) for i, p in enumerate(prompts)])

    try:
        await status.edit_text(f"Готово! Сгенерировано {done['ok']}/10 обложек.")
    except Exception:
        pass


# --- Кнопка «Размножить идею» ---

@dp.callback_query(MultiplyCallback.filter())
async def multiply_idea(query: CallbackQuery, callback_data: MultiplyCallback):
    data = _image_store.get(callback_data.image_id)
    if not data:
        await query.answer("Данные не найдены — перезапустите генерацию.", show_alert=True)
        return

    await query.answer("Генерирую 3 похожих варианта…")
    status = await query.message.answer("Генерирую 3 похожих обложки…")
    prompt = data["prompt"]
    done = {"n": 0, "ok": 0}

    async def gen_and_send(idx: int):
        url = await piapi_client.generate_image(prompt)
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(query.message, url, prompt, f"Размножение {idx}/3")
        else:
            await query.message.answer(f"Размножение {idx}: генерация не удалась.")
        try:
            await status.edit_text(f"Обработано {done['n']}/3 | Готово: {done['ok']}")
        except Exception:
            pass

    await asyncio.gather(*[gen_and_send(i + 1) for i in range(3)])

    try:
        await status.edit_text(f"Готово! Сгенерировано ещё {done['ok']}/3 обложек.")
    except Exception:
        pass


# --- Кнопка «Исправить фотографию» ---

@dp.callback_query(FixCallback.filter())
async def fix_photo_start(query: CallbackQuery, callback_data: FixCallback, state: FSMContext):
    data = _image_store.get(callback_data.image_id)
    if not data:
        await query.answer("Данные не найдены — перезапустите генерацию.", show_alert=True)
        return

    await state.clear()
    await state.update_data(fix_image_id=callback_data.image_id)
    await state.set_state(FixForm.awaiting_correction)
    await query.answer()
    await query.message.answer(
        "Опишите что нужно исправить или добавить.\n"
        "Можно также прикрепить фото-референс с подписью.\n\n"
        "<i>Пример: исправь банку на ту, что на референсе / добавь малярную кисть / измени фон на белый</i>\n\n"
        "Для отмены — /cancel",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(FixForm.awaiting_correction, F.text)
async def fix_with_text(message: Message, state: FSMContext):
    fsm_data = await state.get_data()
    fix_image_id = fsm_data.get("fix_image_id")
    await state.clear()

    image_data = _image_store.get(fix_image_id)
    if not image_data:
        await message.answer("Данные не найдены. Попробуйте нажать кнопку ещё раз.")
        return

    await run_fix_pipeline(message, image_data, message.text.strip(), extra_ref_url=None)


@dp.message(FixForm.awaiting_correction, F.photo)
async def fix_with_photo(message: Message, state: FSMContext):
    fsm_data = await state.get_data()
    fix_image_id = fsm_data.get("fix_image_id")
    await state.clear()

    image_data = _image_store.get(fix_image_id)
    if not image_data:
        await message.answer("Данные не найдены. Попробуйте нажать кнопку ещё раз.")
        return

    correction = message.caption or "Исправь согласно приложенному референсу"
    extra_ref_url = await _tg_url(message.photo[-1].file_id)

    await run_fix_pipeline(message, image_data, correction, extra_ref_url)


async def run_fix_pipeline(
    message: Message,
    image_data: dict,
    correction: str,
    extra_ref_url: str | None,
):
    original_url = image_data["url"]

    fix_prompt = (
        f"Возьми изображение как основу и внеси следующие исправления: {correction}. "
        f"Сохрани общую композицию, стиль и расположение остальных элементов без изменений. "
        f"Вертикальный формат 3:4, современный UX/UI дизайн, "
        f"высококачественная коммерческая обложка для маркетплейса."
    )

    image_urls = [original_url]
    if extra_ref_url:
        image_urls.append(extra_ref_url)

    status = await message.answer("Исправляю изображение…")
    url = await piapi_client.generate_image(fix_prompt, image_urls)

    if url:
        await _send_image(message, url, fix_prompt, "Исправленный вариант")
        try:
            await status.delete()
        except Exception:
            pass
    else:
        await status.edit_text("Не удалось исправить изображение. Попробуйте ещё раз.")


async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
