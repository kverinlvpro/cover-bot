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

_image_store: dict[str, dict] = {}


class CoverForm(StatesGroup):
    # Shared first steps
    ref_photo = State()
    mode_select = State()

    # === Existing card flow ===
    card_url = State()
    confirm_volume = State()
    edit_volume = State()
    utp_select = State()
    card_headline = State()
    card_subtitle = State()

    # === Flexible flow ===
    product_name = State()
    volume = State()
    headline = State()
    subtitle = State()
    badges = State()
    design_request = State()


class FixForm(StatesGroup):
    awaiting_correction = State()


class MultiplyCallback(CallbackData, prefix="mul"):
    image_id: str


class FixCallback(CallbackData, prefix="fix"):
    image_id: str


class VolConfirmCallback(CallbackData, prefix="vc"):
    ok: bool


class UtpToggleCallback(CallbackData, prefix="utptog"):
    idx: int


class UtpDoneCallback(CallbackData, prefix="utpdone"):
    pass


# --- Keyboards ---

def _kb(*labels: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=l) for l in labels]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


RESTART_BTN = "🔄 Заново"
SKIP_KB = _kb("Пропустить", RESTART_BTN)
START_KB = _kb("🚀 Запустить бот")
AGAIN_KB = _kb("🔄 Сгенерировать ещё")
RESTART_KB = _kb(RESTART_BTN)
MODE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔗 На существующую карточку")],
        [KeyboardButton(text="⚙️ Гибкая настройка")],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)


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


def _build_utp_kb(utps: list[str], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, utp in enumerate(utps):
        prefix = "✅" if i in selected else "◻️"
        rows.append([InlineKeyboardButton(
            text=f"{prefix} {utp}",
            callback_data=UtpToggleCallback(idx=i).pack(),
        )])
    rows.append([InlineKeyboardButton(
        text="✅ Подтвердить выбор",
        callback_data=UtpDoneCallback().pack(),
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- /start and /cancel ---

async def _start_form(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Отправьте <b>референсное фото товара</b> (упаковка/банка).\n"
        "Этот шаг обязателен — пропустить нельзя.",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.ref_photo)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Cover Bot — генератор обложек для маркетплейсов</b>\n\n"
        "Нажмите кнопку ниже, чтобы начать создание обложек.",
        parse_mode="HTML",
        reply_markup=START_KB,
    )


@dp.message(F.text.in_({"🚀 Запустить бот", "🔄 Сгенерировать ещё", RESTART_BTN}))
async def btn_start_or_again(message: Message, state: FSMContext):
    await _start_form(message, state)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=START_KB)


# --- Step 1: Reference photo (mandatory) ---

@dp.message(CoverForm.ref_photo, F.photo)
async def step_ref_photo(message: Message, state: FSMContext):
    await state.update_data(photo_ids=[message.photo[-1].file_id])
    await message.answer("Фото получено! Выберите режим:", reply_markup=MODE_KB)
    await state.set_state(CoverForm.mode_select)


@dp.message(CoverForm.ref_photo)
async def step_ref_photo_bad(message: Message):
    await message.answer(
        "Отправьте фото товара (упаковка/банка). Текст не принимается.",
        reply_markup=RESTART_KB,
    )


# --- Step 2: Mode selection ---

@dp.message(CoverForm.mode_select, F.text == "🔗 На существующую карточку")
async def mode_existing_card(message: Message, state: FSMContext):
    await message.answer(
        "Отправьте ссылку на товар (Ozon или Wildberries):",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.card_url)


@dp.message(CoverForm.mode_select, F.text == "⚙️ Гибкая настройка")
async def mode_flexible(message: Message, state: FSMContext):
    await message.answer(
        "Введите <b>название товара</b>:",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.product_name)


# === EXISTING CARD FLOW ===

@dp.message(CoverForm.card_url, F.text)
async def step_card_url(message: Message, state: FSMContext):
    url = message.text.strip()
    status = await message.answer("⏳ Анализирую карточку товара…")

    try:
        analysis = await claude_client.analyze_card(url)
    except Exception as e:
        await status.edit_text(
            f"❌ Не удалось проанализировать карточку:\n{e}\n\nПопробуйте ещё раз."
        )
        return

    name = analysis.get("name", "Неизвестно")
    volume = analysis.get("volume")
    utps = analysis.get("utps", [])

    await state.update_data(product_name=name, utp_list=utps, utp_selected=[])

    await status.edit_text(
        f"✅ Карточка проанализирована\n\n<b>Название:</b> {name}",
        parse_mode="HTML",
    )

    if volume:
        await state.update_data(volume_detected=volume)
        await message.answer(
            f"Объём: <b>{volume}</b> — верно?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Верно", callback_data=VolConfirmCallback(ok=True).pack())],
                [InlineKeyboardButton(text="✏️ Исправить", callback_data=VolConfirmCallback(ok=False).pack())],
            ]),
        )
        await state.set_state(CoverForm.confirm_volume)
    else:
        await message.answer(
            "Объём не найден в заголовке. Введите вручную (например: 360г, 1л):",
            reply_markup=RESTART_KB,
        )
        await state.set_state(CoverForm.edit_volume)


@dp.callback_query(VolConfirmCallback.filter(), CoverForm.confirm_volume)
async def confirm_volume_cb(query: CallbackQuery, callback_data: VolConfirmCallback, state: FSMContext):
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    if callback_data.ok:
        data = await state.get_data()
        await state.update_data(volume=data["volume_detected"])
        await _show_utp_selection(query.message, state)
    else:
        await query.message.answer(
            "Введите правильный объём (например: 360г, 1л):",
            reply_markup=RESTART_KB,
        )
        await state.set_state(CoverForm.edit_volume)


@dp.message(CoverForm.edit_volume, F.text)
async def step_edit_volume(message: Message, state: FSMContext):
    await state.update_data(volume=message.text.strip())
    await _show_utp_selection(message, state)


async def _show_utp_selection(target: Message, state: FSMContext):
    data = await state.get_data()
    utps = data.get("utp_list", [])
    await target.answer(
        "Выберите УТП (преимущества) для обложки.\n"
        "Отметьте нужные и нажмите «Подтвердить»:",
        reply_markup=_build_utp_kb(utps, set()),
    )
    await state.set_state(CoverForm.utp_select)


@dp.callback_query(UtpToggleCallback.filter(), CoverForm.utp_select)
async def utp_toggle(query: CallbackQuery, callback_data: UtpToggleCallback, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("utp_selected", []))
    idx = callback_data.idx
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await state.update_data(utp_selected=list(selected))
    utps = data.get("utp_list", [])
    try:
        await query.message.edit_reply_markup(reply_markup=_build_utp_kb(utps, selected))
    except Exception:
        pass
    await query.answer()


@dp.callback_query(UtpDoneCallback.filter(), CoverForm.utp_select)
async def utp_done(query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("utp_selected", []))
    if not selected:
        await query.answer("Выберите хотя бы одно УТП!", show_alert=True)
        return
    utps = data.get("utp_list", [])
    badges = ", ".join(utps[i] for i in sorted(selected))
    await state.update_data(badges=badges)
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.answer(
        "Введите <b>заголовок</b> — главный текст на обложке:",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.card_headline)


@dp.message(CoverForm.card_headline, F.text)
async def step_card_headline(message: Message, state: FSMContext):
    await state.update_data(headline=message.text.strip())
    await message.answer("Введите <b>подзаголовок</b>:", parse_mode="HTML", reply_markup=RESTART_KB)
    await state.set_state(CoverForm.card_subtitle)


@dp.message(CoverForm.card_subtitle, F.text)
async def step_card_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip(), design_request=None)
    data = await state.get_data()
    await state.clear()
    await message.answer("Принято! Запускаю генерацию…", reply_markup=ReplyKeyboardRemove())
    await run_pipeline(message, data)


# === FLEXIBLE FLOW ===

@dp.message(CoverForm.product_name, F.text)
async def step_product_name(message: Message, state: FSMContext):
    await state.update_data(product_name=message.text.strip())
    await message.answer(
        "Введите <b>объём товара</b> (например: 360г, 1л, 500мл):",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.volume)


@dp.message(CoverForm.volume, F.text)
async def step_volume(message: Message, state: FSMContext):
    await state.update_data(volume=message.text.strip())
    await message.answer(
        "Введите <b>заголовок</b> — главный текст на обложке:",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.headline)


@dp.message(CoverForm.headline, F.text)
async def step_headline(message: Message, state: FSMContext):
    await state.update_data(headline=message.text.strip())
    await message.answer(
        "Введите <b>подзаголовок</b>:",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.subtitle)


@dp.message(CoverForm.subtitle, F.text)
async def step_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await message.answer(
        "Введите <b>плашки свойств</b> — преимущества через запятую:\n"
        "<i>Пример: улучшает сцепление, для любых поверхностей, быстро сохнет</i>",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
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
    await state.update_data(design_request=None if text == "Пропустить" else text)
    data = await state.get_data()
    await state.clear()
    await message.answer("Принято! Запускаю генерацию…", reply_markup=ReplyKeyboardRemove())
    await run_pipeline(message, data)


# --- Utilities ---

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


# --- Main pipeline ---

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
        try:
            url = await piapi_client.generate_image(prompt, ref_urls or None)
        except Exception as e:
            logging.error("generate_image idx=%d error: %s", idx, e)
            url = None
        done["n"] += 1
        if url:
            done["ok"] += 1
            try:
                await _send_image(message, url, prompt, f"Вариант {idx}/10")
            except Exception as e:
                logging.error("_send_image idx=%d error: %s", idx, e)
        else:
            try:
                await message.answer(f"Вариант {idx}: генерация не удалась.")
            except Exception:
                pass
        try:
            await status.edit_text(f"Обработано {done['n']}/10 | Готово: {done['ok']}")
        except Exception:
            pass

    try:
        await asyncio.gather(*[gen_and_send(i + 1, p) for i, p in enumerate(prompts)])
    except Exception as e:
        logging.error("gather error: %s", e)

    try:
        await status.edit_text(f"Готово! Сгенерировано {done['ok']}/10 обложек.")
    except Exception:
        pass
    try:
        await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)
    except Exception as e:
        logging.error("AGAIN_KB send error: %s", e)


# --- Multiply idea ---

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


# --- Fix photo ---

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
        "<i>Пример: исправь банку / добавь малярную кисть / измени фон на белый</i>\n\n"
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
