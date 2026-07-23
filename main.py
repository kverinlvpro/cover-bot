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
import sheets_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

bot = Bot(token=config.TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_image_store: dict[str, dict] = {}


class CoverForm(StatesGroup):
    # Shared first steps
    ref_photo = State()
    mode_select = State()

    # === DB search flow ===
    product_search = State()        # user types search query
    missing_rgb = State()           # RGB absent in DB: manual / from photo / skip
    manual_rgb_input = State()      # user types RGB manually
    missing_field_input = State()   # user types missing volume or UTPs
    utp_select = State()
    manual_utp_add = State()
    card_headline = State()
    card_subtitle = State()

    # === Flexible flow ===
    paint_type_select = State()
    flexible_color_samples = State()
    color_code = State()            # optional color code step (flexible, wall paint)
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


class UtpToggleCallback(CallbackData, prefix="utptog"):
    idx: int


class UtpDoneCallback(CallbackData, prefix="utpdone"):
    pass


class UtpAddCallback(CallbackData, prefix="utpadd"):
    pass


class ProductSelectCallback(CallbackData, prefix="psel"):
    idx: int


# --- Keyboards ---

def _kb(*labels: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=l) for l in labels]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


RESTART_BTN = "🔄 Заново"
BACK_BTN = "⬅️ Назад"
SKIP_KB = _kb("Пропустить", RESTART_BTN)
START_KB = _kb("🚀 Запустить бот")
AGAIN_KB = _kb("🔄 Сгенерировать ещё")
RESTART_KB = _kb(RESTART_BTN)
BACK_RESTART_KB = _kb(BACK_BTN, RESTART_BTN)
BACK_SKIP_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Пропустить")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

MODE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Выбрать из базы")],
        [KeyboardButton(text="⚙️ Гибкая настройка")],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

PAINT_TYPE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪑 Краска для мебели")],
        [KeyboardButton(text="🏠 Краска для стен")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

COLOR_SAMPLES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Готово")],
        [KeyboardButton(text="Пропустить")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

COLOR_CODE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Пропустить")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

MISSING_RGB_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✏️ Ввести RGB вручную")],
        [KeyboardButton(text="📸 Взять с фото банки")],
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

MISSING_DATA_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✏️ Ввести вручную")],
        [KeyboardButton(text=BACK_BTN)],
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
        text="✏️ Вписать свои УТП",
        callback_data=UtpAddCallback().pack(),
    )])
    rows.append([InlineKeyboardButton(
        text="✅ Подтвердить выбор",
        callback_data=UtpDoneCallback().pack(),
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _product_btn_text(p: dict) -> str:
    parts = [p["name"]]
    if p["volume"]:
        parts.append(p["volume"])
    if p["color_name"]:
        parts.append(f"| {p['color_name']}")
    text = " ".join(parts)
    return text[:64]


def _build_search_kb(results: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(results):
        rows.append([InlineKeyboardButton(
            text=_product_btn_text(p),
            callback_data=ProductSelectCallback(idx=i).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Universal back navigation (must be registered FIRST) ---

@dp.message(F.text == BACK_BTN)
async def handle_back(message: Message, state: FSMContext):
    current = await state.get_state()
    data = await state.get_data()

    # DB flow
    if current == CoverForm.product_search.state:
        await message.answer("Выберите режим:", reply_markup=MODE_KB)
        await state.set_state(CoverForm.mode_select)

    elif current in (CoverForm.missing_rgb.state, CoverForm.missing_field_input.state):
        results = data.get("search_results", [])
        if results:
            await message.answer("Выберите товар:", reply_markup=_build_search_kb(results))
        else:
            await message.answer("Введите название для поиска:", reply_markup=RESTART_KB)
        await state.set_state(CoverForm.product_search)

    elif current == CoverForm.manual_rgb_input.state:
        await message.answer("Как поступим с цветом?", reply_markup=MISSING_RGB_KB)
        await state.set_state(CoverForm.missing_rgb)

    elif current == CoverForm.manual_utp_add.state:
        utps = data.get("utp_list", [])
        selected = set(data.get("utp_selected", []))
        await message.answer("Выберите УТП:", reply_markup=_build_utp_kb(utps, selected))
        await state.set_state(CoverForm.utp_select)

    elif current == CoverForm.card_headline.state:
        utps = data.get("utp_list", [])
        selected = set(data.get("utp_selected", []))
        await message.answer(
            "Выберите УТП для обложки — снимите галочки с ненужных:",
            reply_markup=_build_utp_kb(utps, selected),
        )
        await state.set_state(CoverForm.utp_select)

    elif current == CoverForm.card_subtitle.state:
        await message.answer(
            "Введите <b>заголовок</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.card_headline)

    # Shared design_request
    elif current == CoverForm.design_request.state:
        flow = data.get("flow", "db")
        if flow == "flexible":
            await message.answer(
                "Введите <b>плашки свойств</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
            )
            await state.set_state(CoverForm.badges)
        else:
            await message.answer(
                "Введите <b>подзаголовок</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
            )
            await state.set_state(CoverForm.card_subtitle)

    # Flexible flow
    elif current == CoverForm.paint_type_select.state:
        await message.answer("Выберите режим:", reply_markup=MODE_KB)
        await state.set_state(CoverForm.mode_select)

    elif current == CoverForm.flexible_color_samples.state:
        await message.answer("Выберите тип краски:", reply_markup=PAINT_TYPE_KB)
        await state.set_state(CoverForm.paint_type_select)

    elif current == CoverForm.color_code.state:
        await message.answer(
            "Загрузите образец цвета или нажмите «Пропустить»:",
            reply_markup=COLOR_SAMPLES_KB,
        )
        await state.set_state(CoverForm.flexible_color_samples)

    elif current == CoverForm.product_name.state:
        paint_type = data.get("paint_type", "furniture")
        if paint_type == "walls":
            await message.answer("Введите код цвета:", reply_markup=COLOR_CODE_KB)
            await state.set_state(CoverForm.color_code)
        else:
            await message.answer("Выберите тип краски:", reply_markup=PAINT_TYPE_KB)
            await state.set_state(CoverForm.paint_type_select)

    elif current == CoverForm.volume.state:
        await message.answer(
            "Введите <b>название товара</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.product_name)

    elif current == CoverForm.headline.state:
        await message.answer(
            "Введите <b>объём товара</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.volume)

    elif current == CoverForm.subtitle.state:
        await message.answer(
            "Введите <b>заголовок</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.headline)

    elif current == CoverForm.badges.state:
        await message.answer(
            "Введите <b>подзаголовок</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.subtitle)

    else:
        await message.answer("На этом шаге вернуться назад нельзя.", reply_markup=RESTART_KB)


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

@dp.message(CoverForm.mode_select, F.text == "📋 Выбрать из базы")
async def mode_db(message: Message, state: FSMContext):
    await state.update_data(flow="db")
    await message.answer(
        "Введите название товара или линейки для поиска:\n"
        "<i>Например: Velvet, MIA, Classic</i>",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.product_search)


@dp.message(CoverForm.mode_select, F.text == "⚙️ Гибкая настройка")
async def mode_flexible(message: Message, state: FSMContext):
    await state.update_data(flow="flexible")
    await message.answer("Выберите тип краски:", reply_markup=PAINT_TYPE_KB)
    await state.set_state(CoverForm.paint_type_select)


# === DB SEARCH FLOW ===

@dp.message(CoverForm.product_search, F.text)
async def step_product_search(message: Message, state: FSMContext):
    query = message.text.strip()
    status = await message.answer("🔍 Ищу в базе…")

    try:
        products = await sheets_client.load_products()
    except Exception as e:
        await status.edit_text(f"❌ Не удалось загрузить базу: {e}")
        return

    results = sheets_client.search_products(query, products)

    if not results:
        await status.edit_text(
            f"Ничего не найдено по запросу «{query}».\nПопробуйте другое название.",
        )
        return

    if len(results) > 10:
        await status.edit_text(
            f"Найдено {len(results)} позиций — слишком много.\n"
            f"Уточните запрос (добавьте объём или цвет)."
        )
        return

    await state.update_data(search_results=results)
    await status.edit_text(
        f"Найдено {len(results)} позиций. Выберите товар:",
        reply_markup=_build_search_kb(results),
    )


@dp.callback_query(ProductSelectCallback.filter(), CoverForm.product_search)
async def product_select_cb(
    query: CallbackQuery,
    callback_data: ProductSelectCallback,
    state: FSMContext,
):
    await query.answer()
    data = await state.get_data()
    results: list[dict] = data.get("search_results", [])
    idx = callback_data.idx

    if idx >= len(results):
        await query.message.answer("Ошибка выбора, попробуйте снова.", reply_markup=RESTART_KB)
        return

    product = results[idx]
    paint_type = product["paint_type"]

    await state.update_data(
        product_name=product["name"],
        paint_type=paint_type,
        color_photo_ids=[],
        color_code=product["rgb"] if product["rgb"] else None,
        color_name=product["color_name"],
        utp_list=product["utps"],
        utp_selected=list(range(len(product["utps"]))),  # pre-select all
    )

    paint_label = "🏠 для стен" if paint_type == "walls" else "🪑 для мебели"
    color_info = f"\n<b>Цвет:</b> {product['color_name']}" if product["color_name"] else ""
    rgb_info = f"\n<b>RGB:</b> {product['rgb']}" if product["rgb"] else ""

    await query.message.answer(
        f"✅ <b>{product['name']}</b>\n"
        f"<b>Тип:</b> {paint_label}"
        f"{color_info}{rgb_info}",
        parse_mode="HTML",
    )

    await _continue_db_flow(query.message, state, product)


async def _continue_db_flow(message: Message, state: FSMContext, product: dict):
    """Check missing fields and route to the right step."""
    # 1. Volume missing?
    if not product["volume"]:
        await message.answer(
            "⚠️ Объём не указан в базе для этого товара.",
            reply_markup=MISSING_DATA_KB,
        )
        await state.update_data(missing_field="volume")
        await state.set_state(CoverForm.missing_field_input)
        return

    await state.update_data(volume=product["volume"])

    # 2. UTPs missing?
    if not product["utps"]:
        await message.answer(
            "⚠️ УТП не указаны в базе для этого товара.",
            reply_markup=MISSING_DATA_KB,
        )
        await state.update_data(missing_field="utps")
        await state.set_state(CoverForm.missing_field_input)
        return

    # 3. Wall paint + RGB missing?
    if product["paint_type"] == "walls" and not product["rgb"]:
        await message.answer(
            "⚠️ Код цвета RGB отсутствует в базе.\nКак поступим?",
            reply_markup=MISSING_RGB_KB,
        )
        await state.set_state(CoverForm.missing_rgb)
        return

    # All good — proceed to UTP selection
    await _show_utp_selection(message, state)


# --- Missing field input (volume or UTPs) ---

@dp.message(CoverForm.missing_field_input, F.text == "✏️ Ввести вручную")
async def missing_field_manual(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("missing_field")
    if field == "volume":
        await message.answer("Введите объём (например: 2.5л, 800г):", reply_markup=RESTART_KB)
    else:
        await message.answer(
            "Введите УТП через запятую:\n"
            "<i>Пример: Моющаяся, Без запаха, Быстросохнущая</i>",
            parse_mode="HTML",
            reply_markup=RESTART_KB,
        )


@dp.message(CoverForm.missing_field_input, F.text)
async def step_missing_field(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("missing_field")
    text = message.text.strip()

    if field == "volume":
        await state.update_data(volume=text)
        # Check if UTPs are also missing
        if not data.get("utp_list"):
            await message.answer(
                "⚠️ УТП не указаны в базе для этого товара.",
                reply_markup=MISSING_DATA_KB,
            )
            await state.update_data(missing_field="utps")
            return
    else:  # utps
        utps = [u.strip() for u in text.split(",") if u.strip()]
        await state.update_data(utp_list=utps, utp_selected=list(range(len(utps))))

    paint_type = data.get("paint_type", "furniture")
    rgb = data.get("color_code") or ""
    if paint_type == "walls" and not rgb:
        await message.answer(
            "⚠️ Код цвета RGB отсутствует в базе.\nКак поступим?",
            reply_markup=MISSING_RGB_KB,
        )
        await state.set_state(CoverForm.missing_rgb)
        return

    await _show_utp_selection(message, state)


# --- Missing RGB handlers ---

@dp.message(CoverForm.missing_rgb, F.text == "✏️ Ввести RGB вручную")
async def missing_rgb_manual(message: Message, state: FSMContext):
    await message.answer(
        "Введите RGB в формате <b>XXX,XXX,XXX</b>:\n"
        "<i>Пример: 245,240,232</i>",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.manual_rgb_input)


@dp.message(CoverForm.missing_rgb, F.text.in_({"📸 Взять с фото банки", "⏭ Пропустить"}))
async def missing_rgb_skip(message: Message, state: FSMContext):
    # No explicit RGB — Claude will use reference photo for color
    await state.update_data(color_code=None)
    await _show_utp_selection(message, state)


@dp.message(CoverForm.missing_rgb)
async def missing_rgb_bad(message: Message):
    await message.answer("Выберите вариант с помощью кнопок:", reply_markup=MISSING_RGB_KB)


@dp.message(CoverForm.manual_rgb_input, F.text)
async def step_manual_rgb(message: Message, state: FSMContext):
    rgb = message.text.strip()
    await state.update_data(color_code=rgb)
    await _show_utp_selection(message, state)


# --- UTP selection (shared between DB and card flows) ---

async def _show_utp_selection(target: Message, state: FSMContext):
    data = await state.get_data()
    utps = data.get("utp_list", [])
    selected = set(data.get("utp_selected", []))
    await target.answer(
        "Выберите УТП для обложки — снимите галочки с ненужных и нажмите «Подтвердить»:",
        reply_markup=_build_utp_kb(utps, selected),
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


@dp.callback_query(UtpAddCallback.filter(), CoverForm.utp_select)
async def utp_add_start(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await query.message.answer(
        "Введите свои УТП через запятую — они добавятся к списку:\n"
        "<i>Пример: Без запаха, Моющаяся, Быстросохнущая</i>",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.manual_utp_add)


@dp.message(CoverForm.manual_utp_add, F.text)
async def step_manual_utp_add(message: Message, state: FSMContext):
    new_utps = [u.strip() for u in message.text.split(",") if u.strip()]
    data = await state.get_data()
    utps: list[str] = list(data.get("utp_list", []))
    selected: set = set(data.get("utp_selected", []))

    start_idx = len(utps)
    utps.extend(new_utps)
    for i in range(start_idx, len(utps)):
        selected.add(i)

    await state.update_data(utp_list=utps, utp_selected=list(selected))
    await state.set_state(CoverForm.utp_select)
    await message.answer(
        f"Добавлено {len(new_utps)} УТП. Проверьте список и подтвердите выбор:",
        reply_markup=_build_utp_kb(utps, selected),
    )


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
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(CoverForm.card_headline)


@dp.message(CoverForm.card_headline, F.text)
async def step_card_headline(message: Message, state: FSMContext):
    await state.update_data(headline=message.text.strip())
    await message.answer("Введите <b>подзаголовок</b>:", parse_mode="HTML", reply_markup=BACK_RESTART_KB)
    await state.set_state(CoverForm.card_subtitle)


@dp.message(CoverForm.card_subtitle, F.text)
async def step_card_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await message.answer(
        "Введите <b>дизайнерский запрос</b> — особая деталь на каждой обложке:\n"
        "<i>Пример: малярная кисть, фото ДО/ПОСЛЕ, живые цветы</i>\n\n"
        "Или нажмите «Пропустить»",
        parse_mode="HTML",
        reply_markup=BACK_SKIP_KB,
    )
    await state.set_state(CoverForm.design_request)


# === FLEXIBLE FLOW ===

@dp.message(CoverForm.paint_type_select, F.text.in_({"🪑 Краска для мебели", "🏠 Краска для стен"}))
async def step_paint_type_select(message: Message, state: FSMContext):
    paint_type = "walls" if "стен" in message.text else "furniture"
    await state.update_data(paint_type=paint_type, color_photo_ids=[])

    if paint_type == "walls":
        await message.answer(
            "🎨 <b>Краска для стен</b> — загрузите образец цвета и живые фото краски.\n"
            "Можно отправить до 4 фото по одному.\n"
            "Когда всё загружено — нажмите «Готово».\n"
            "Или нажмите «Пропустить».",
            parse_mode="HTML",
            reply_markup=COLOR_SAMPLES_KB,
        )
        await state.set_state(CoverForm.flexible_color_samples)
    else:
        await message.answer(
            "Введите <b>название товара</b>:",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.product_name)


@dp.message(CoverForm.paint_type_select)
async def step_paint_type_bad(message: Message):
    await message.answer("Выберите тип краски с помощью кнопок:", reply_markup=PAINT_TYPE_KB)


@dp.message(CoverForm.flexible_color_samples, F.photo)
async def flexible_color_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    ids = list(data.get("color_photo_ids", []))
    if len(ids) >= 4:
        await message.answer(
            "Достигнут лимит — 4 фото. Нажмите «Готово» для продолжения.",
            reply_markup=COLOR_SAMPLES_KB,
        )
        return
    ids.append(message.photo[-1].file_id)
    await state.update_data(color_photo_ids=ids)
    await message.answer(
        f"Фото {len(ids)} загружено. Добавьте ещё или нажмите «Готово».",
        reply_markup=COLOR_SAMPLES_KB,
    )


@dp.message(CoverForm.flexible_color_samples, F.text.in_({"✅ Готово", "Пропустить"}))
async def flexible_color_done(message: Message, state: FSMContext):
    if message.text == "Пропустить":
        await state.update_data(color_photo_ids=[])
    await message.answer(
        "Введите <b>код цвета краски</b> — или нажмите «Пропустить».\n\n"
        "Форматы: RGB <b>245,240,232</b> · RAL 9001 · Pantone 11-0602 TCX",
        parse_mode="HTML",
        reply_markup=COLOR_CODE_KB,
    )
    await state.set_state(CoverForm.color_code)


@dp.message(CoverForm.flexible_color_samples)
async def flexible_color_bad(message: Message):
    await message.answer(
        "Отправьте фото или нажмите «Готово» / «Пропустить».",
        reply_markup=COLOR_SAMPLES_KB,
    )


@dp.message(CoverForm.color_code, F.text)
async def step_color_code(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(color_code=None if text == "Пропустить" else text)
    await message.answer(
        "Введите <b>название товара</b>:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(CoverForm.product_name)


@dp.message(CoverForm.product_name, F.text)
async def step_product_name(message: Message, state: FSMContext):
    await state.update_data(product_name=message.text.strip())
    await message.answer(
        "Введите <b>объём товара</b> (например: 360г, 1л, 500мл):",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(CoverForm.volume)


@dp.message(CoverForm.volume, F.text)
async def step_volume(message: Message, state: FSMContext):
    await state.update_data(volume=message.text.strip())
    await message.answer(
        "Введите <b>заголовок</b> — главный текст на обложке:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(CoverForm.headline)


@dp.message(CoverForm.headline, F.text)
async def step_headline(message: Message, state: FSMContext):
    await state.update_data(headline=message.text.strip())
    await message.answer(
        "Введите <b>подзаголовок</b>:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(CoverForm.subtitle)


@dp.message(CoverForm.subtitle, F.text)
async def step_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await message.answer(
        "Введите <b>плашки свойств</b> — преимущества через запятую:\n"
        "<i>Пример: улучшает сцепление, для любых поверхностей, быстро сохнет</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
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
        reply_markup=BACK_SKIP_KB,
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
    color_code = data.get("color_code")
    color_name = data.get("color_name", "")
    paint_type = data.get("paint_type", "furniture")

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
    if color_code or color_name:
        name_part = f"«{color_name}»" if color_name else ""
        rgb_part = f"RGB({color_code})" if color_code else ""
        tech_hint = f" (точный оттенок для нейросети: {rgb_part})" if rgb_part else ""
        display = name_part or rgb_part
        name_on_cover = f"«{color_name}»" if color_name else ""
        no_rgb_note = f" На обложке пишется только красивое название {name_on_cover}, RGB-код нигде не указывается." if color_name else " RGB-код на обложке не пишется."
        if paint_type == "walls":
            points.append(
                f"{len(points) + 1}) Цвет краски: {display}{tech_hint} — "
                f"окрашенные поверхности строго этого оттенка.{no_rgb_note}"
            )
        else:
            points.append(
                f"{len(points) + 1}) Цвет краски: {display}{tech_hint} — "
                f"оттенок на окрашенной поверхности и банке точно соответствует.{no_rgb_note}"
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
    color_photo_ids: list[str] = data.get("color_photo_ids", [])
    paint_type: str = data.get("paint_type", "furniture")

    status = await message.answer("Генерирую промты через Claude…")

    image_bytes: bytes | None = None
    if photo_ids:
        try:
            file = await bot.get_file(photo_ids[0])
            buf = await bot.download_file(file.file_path)
            image_bytes = buf.read()
        except Exception:
            pass

    color_image_bytes: list[bytes] = []
    if paint_type == "walls" and color_photo_ids:
        for fid in color_photo_ids[:4]:
            try:
                file = await bot.get_file(fid)
                buf = await bot.download_file(file.file_path)
                color_image_bytes.append(buf.read())
            except Exception:
                pass

    if paint_type == "walls" and color_image_bytes:
        try:
            await status.edit_text("Анализирую оттенок краски…")
            color_description = await claude_client.analyze_color_samples(color_image_bytes)
            logging.info("color_description: %s", color_description)
            user_request += f"\n\nТочный оттенок краски (определён по образцам): {color_description}"
        except Exception as e:
            logging.warning("analyze_color_samples failed: %s", e)

    try:
        prompts = await claude_client.generate_prompts(
            user_request,
            image_bytes,
            color_image_bytes or None,
            paint_type,
        )
    except Exception as e:
        await status.edit_text(f"Ошибка генерации промтов: {e}")
        await message.answer("Хотите попробовать ещё раз?", reply_markup=AGAIN_KB)
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
        await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)
    else:
        await status.edit_text("Не удалось исправить изображение. Попробуйте ещё раз.")


async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
