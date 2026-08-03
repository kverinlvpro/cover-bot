import asyncio
import logging
import re
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
_scale_data: dict[str, dict] = {}   # image_id -> {products, selected}


class CoverForm(StatesGroup):
    # Shared first steps
    content_type_select = State()   # Обложка vs Внутренние слайды
    ref_photo = State()
    mode_select = State()

    # === DB search flow ===
    product_search = State()        # user types search query
    missing_rgb = State()           # RGB absent in DB: manual / from photo / skip
    manual_rgb_input = State()      # user types RGB manually
    missing_field_input = State()   # user types missing volume or UTPs
    utp_select = State()
    manual_utp_add = State()
    volume_unit_select = State()
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


class MultiplyFeedbackForm(StatesGroup):
    awaiting_feedback = State()
    awaiting_new_ref = State()


class SlideForm(StatesGroup):
    type_select = State()           # с референса / с ноля
    competitor_slide = State()      # фото слайда конкурента (только "с референса")
    product_search = State()        # поиск товара в базе
    render_photo = State()          # рендер банки краски
    design_request = State()        # дизайнерский запрос
    text_content = State()          # что написать на слайде
    post_gen = State()              # после генерации: ещё / изменить запрос / заново
    new_design_request = State()    # изменить дизайнерский запрос


class BannerForm(StatesGroup):
    product_search = State()        # поиск товара в базе
    render_photo = State()          # рендер банки краски
    design_request = State()        # дизайнерский запрос (что показать)
    headline = State()              # большой заголовок
    subtitle = State()              # мелкий подзаголовок
    post_gen = State()              # после генерации
    new_design_request = State()    # изменить дизайнерский запрос


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


class SlideProductSelectCallback(CallbackData, prefix="spsel"):
    idx: int


class BannerProductSelectCallback(CallbackData, prefix="bpsel"):
    idx: int


class ScaleColorsCB(CallbackData, prefix="sc"):
    action: str   # start | toggle | confirm | cancel
    image_id: str
    idx: int = -1


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

PRODUCT_TYPE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🖼 Обложка для маркетплейса")],
        [KeyboardButton(text="📑 Внутренние слайды")],
        [KeyboardButton(text="🎯 Создать баннер")],
    ],
    resize_keyboard=True,
)

SLIDE_TYPE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📎 С референса конкурента")],
        [KeyboardButton(text="✨ С ноля")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

SLIDE_POST_GEN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Ещё такие же")],
        [KeyboardButton(text="✏️ Изменить дизайнерский запрос")],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

BANNER_POST_GEN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Ещё такие же баннеры")],
        [KeyboardButton(text="✏️ Изменить дизайнерский запрос")],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

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
        [KeyboardButton(text="✨ Лак для мебели")],
        [KeyboardButton(text="🔩 Грунтовка для мебели")],
        [KeyboardButton(text=BACK_BTN)],
        [KeyboardButton(text=RESTART_BTN)],
    ],
    resize_keyboard=True,
)

_PAINT_TYPE_OPTIONS = {
    "🪑 Краска для мебели": "furniture",
    "🏠 Краска для стен": "walls",
    "✨ Лак для мебели": "lacquer",
    "🔩 Грунтовка для мебели": "primer",
}

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


def _is_weight(volume: str) -> bool:
    return bool(re.search(r'\d[\d.,]*\s*(?:кг|г)\b', volume.lower()))


def _convert_weight(volume: str, target: str) -> str:
    m = re.match(r'^\s*([\d.,]+)\s*(кг|г)\s*$', volume.strip(), re.IGNORECASE)
    if not m:
        return volume
    try:
        num = float(m.group(1).replace(',', '.'))
        unit = m.group(2).lower()
    except ValueError:
        return volume
    if target == 'г' and unit == 'кг':
        return f"{int(num * 1000)} г"
    if target == 'кг' and unit == 'г':
        kg = num / 1000
        formatted = f"{kg:.3g}".rstrip('0').rstrip('.')
        return f"{formatted} кг"
    return volume


def _volume_unit_kb(volume: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"✅ Как есть ({volume})")],
            [KeyboardButton(text="⚖️ В кг")],
            [KeyboardButton(text="⚖️ В граммах")],
            [KeyboardButton(text=RESTART_BTN)],
        ],
        resize_keyboard=True,
    )


def _image_kb(image_id: str, has_line: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="🔁 Размножить идею",
            callback_data=MultiplyCallback(image_id=image_id).pack(),
        )],
        [InlineKeyboardButton(
            text="✏️ Исправить фотографию",
            callback_data=FixCallback(image_id=image_id).pack(),
        )],
    ]
    if has_line:
        rows.append([InlineKeyboardButton(
            text="🎨 Цвета линейки (beta)",
            callback_data=ScaleColorsCB(action="start", image_id=image_id).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_scale_kb(image_id: str, products: list[dict], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(products):
        mark = "☑️" if i in selected else "⬜"
        name = p.get("color_name") or p.get("rgb", "?")
        rows.append([InlineKeyboardButton(
            text=f"{mark} {name}",
            callback_data=ScaleColorsCB(action="toggle", image_id=image_id, idx=i).pack(),
        )])
    confirm_text = f"✅ Генерировать ({len(selected)} цв.)" if selected else "✅ Генерировать все"
    rows.append([
        InlineKeyboardButton(
            text=confirm_text,
            callback_data=ScaleColorsCB(action="confirm", image_id=image_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=ScaleColorsCB(action="cancel", image_id=image_id).pack(),
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    # Slides flow
    elif current == SlideForm.type_select.state:
        await message.answer("Что хотите создать?", reply_markup=PRODUCT_TYPE_KB)
        await state.set_state(CoverForm.content_type_select)

    elif current == SlideForm.competitor_slide.state:
        await message.answer("Выберите способ создания слайда:", reply_markup=SLIDE_TYPE_KB)
        await state.set_state(SlideForm.type_select)

    elif current == SlideForm.product_search.state:
        if data.get("slide_with_reference"):
            await message.answer(
                "Отправьте <b>фото внутреннего слайда конкурента</b>:",
                parse_mode="HTML",
                reply_markup=BACK_RESTART_KB,
            )
            await state.set_state(SlideForm.competitor_slide)
        else:
            await message.answer("Выберите способ создания слайда:", reply_markup=SLIDE_TYPE_KB)
            await state.set_state(SlideForm.type_select)

    elif current == SlideForm.render_photo.state:
        await message.answer(
            "Введите название товара или линейки для поиска в базе:",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(SlideForm.product_search)

    elif current == SlideForm.design_request.state:
        await message.answer(
            "Отправьте <b>рендер банки краски</b>:",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(SlideForm.render_photo)

    elif current == SlideForm.text_content.state:
        await message.answer(
            "Что нужно <b>показать</b> на слайде?",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(SlideForm.design_request)

    elif current == SlideForm.new_design_request.state:
        await message.answer("Что дальше?", reply_markup=SLIDE_POST_GEN_KB)
        await state.set_state(SlideForm.post_gen)

    # Banner flow
    elif current == BannerForm.product_search.state:
        await message.answer("Что хотите создать?", reply_markup=PRODUCT_TYPE_KB)
        await state.set_state(CoverForm.content_type_select)

    elif current == BannerForm.render_photo.state:
        await message.answer(
            "Введите название товара или линейки для поиска в базе:",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(BannerForm.product_search)

    elif current == BannerForm.design_request.state:
        await message.answer(
            "Отправьте <b>рендер банки краски</b>:",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(BannerForm.render_photo)

    elif current == BannerForm.headline.state:
        await message.answer(
            "Что нужно <b>показать</b> на баннере?",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(BannerForm.design_request)

    elif current == BannerForm.subtitle.state:
        await message.answer(
            "Введите <b>большой заголовок</b> баннера:",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(BannerForm.headline)

    elif current == BannerForm.new_design_request.state:
        await message.answer("Что дальше?", reply_markup=BANNER_POST_GEN_KB)
        await state.set_state(BannerForm.post_gen)

    else:
        await message.answer("На этом шаге вернуться назад нельзя.", reply_markup=RESTART_KB)


# --- /start and /cancel ---

async def _start_form(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Что хотите создать?",
        reply_markup=PRODUCT_TYPE_KB,
    )
    await state.set_state(CoverForm.content_type_select)


@dp.message(CoverForm.content_type_select, F.text == "🖼 Обложка для маркетплейса")
async def product_type_cover(message: Message, state: FSMContext):
    await message.answer(
        "Отправьте <b>референсное фото товара</b> (упаковка/банка).\n"
        "Этот шаг обязателен — пропустить нельзя.",
        parse_mode="HTML",
        reply_markup=RESTART_KB,
    )
    await state.set_state(CoverForm.ref_photo)


@dp.message(CoverForm.content_type_select, F.text == "📑 Внутренние слайды")
async def product_type_slides(message: Message, state: FSMContext):
    await message.answer(
        "Выберите способ создания слайда:",
        reply_markup=SLIDE_TYPE_KB,
    )
    await state.set_state(SlideForm.type_select)


@dp.message(CoverForm.content_type_select, F.text == "🎯 Создать баннер")
async def product_type_banner(message: Message, state: FSMContext):
    await message.answer(
        "Введите название товара или линейки для поиска в базе:",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(BannerForm.product_search)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Cover Bot — генератор контента для маркетплейсов</b>\n\n"
        "Нажмите кнопку ниже, чтобы начать.",
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


async def _continue_db_after_volume(message: Message, state: FSMContext):
    """Continue DB flow after volume is confirmed (check UTPs → RGB → UTP selection)."""
    data = await state.get_data()
    if not data.get("utp_list"):
        await message.answer(
            "⚠️ УТП не указаны в базе для этого товара.",
            reply_markup=MISSING_DATA_KB,
        )
        await state.update_data(missing_field="utps")
        await state.set_state(CoverForm.missing_field_input)
        return
    if data.get("paint_type") == "walls" and not (data.get("color_code") or ""):
        await message.answer(
            "⚠️ Код цвета RGB отсутствует в базе.\nКак поступим?",
            reply_markup=MISSING_RGB_KB,
        )
        await state.set_state(CoverForm.missing_rgb)
        return
    await _show_utp_selection(message, state)


async def _ask_volume_unit_or_continue(message: Message, state: FSMContext):
    """After volume is set: ask unit if it's a weight, else continue the flow."""
    data = await state.get_data()
    volume = data.get("volume", "")
    if _is_weight(volume):
        await message.answer(
            f"Как писать вес на обложке?",
            reply_markup=_volume_unit_kb(volume),
        )
        await state.set_state(CoverForm.volume_unit_select)
    else:
        await _after_volume_unit_confirmed(message, state)


async def _after_volume_unit_confirmed(message: Message, state: FSMContext):
    """Continue after volume unit is confirmed."""
    data = await state.get_data()
    if data.get("flow") == "flexible":
        await message.answer(
            "Введите <b>заголовок</b> — главный текст на обложке:",
            parse_mode="HTML",
            reply_markup=BACK_RESTART_KB,
        )
        await state.set_state(CoverForm.headline)
    else:
        await _continue_db_after_volume(message, state)


@dp.message(CoverForm.volume_unit_select, F.text)
async def step_volume_unit_select(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    volume = data.get("volume", "")
    if "граммах" in text or text == "⚖️ В граммах":
        volume = _convert_weight(volume, "г")
    elif "кг" in text and "как есть" not in text.lower():
        volume = _convert_weight(volume, "кг")
    await state.update_data(volume=volume)
    await _after_volume_unit_confirmed(message, state)


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
    await _ask_volume_unit_or_continue(message, state)


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
        await _ask_volume_unit_or_continue(message, state)
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

@dp.message(CoverForm.paint_type_select, F.text.in_(_PAINT_TYPE_OPTIONS))
async def step_paint_type_select(message: Message, state: FSMContext):
    paint_type = _PAINT_TYPE_OPTIONS[message.text]
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
    await state.update_data(volume=message.text.strip(), flow="flexible")
    await _ask_volume_unit_or_continue(message, state)


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

def _detect_lacquer_finish(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("полуматов", "шелковист", "satin", "полу-матов")):
        return "полуматовый"
    if any(w in t for w in ("полуглянц", "полу-глянц")):
        return "полуглянцевый"
    if any(w in t for w in ("матов", "matte")):
        return "матовый"
    if any(w in t for w in ("глянц", "gloss")):
        return "глянцевый"
    return ""


def _swap_color_in_prompt(prompt: str, old_code: str, old_name: str, new_code: str, new_name: str) -> str:
    result = prompt
    if old_name:
        new_name_str = f"«{new_name}»" if new_name else (f"RGB({new_code})" if new_code else "?")
        result = result.replace(f"«{old_name}»", new_name_str)
    if old_code and new_code:
        result = result.replace(f"RGB({old_code})", f"RGB({new_code})")
    return result


def _build_slide_request(data: dict) -> str:
    product = data.get("product_name", "")
    paint_type = data.get("paint_type", "furniture")
    color_name = data.get("color_name", "")
    color_code = data.get("color_code", "")
    design_req = data.get("slide_design_request", "")
    text_content = data.get("slide_text_content", "")
    has_competitor = bool(data.get("slide_competitor_fid"))

    paint_labels = {
        "walls": "краска для стен", "lacquer": "лак для мебели",
        "primer": "грунтовка для мебели", "furniture": "краска для мебели",
    }
    paint_label = paint_labels.get(paint_type, paint_type)

    color_part = ""
    if color_name:
        rgb_hint = f" (точный RGB: {color_code})" if color_code else ""
        color_part = f"Цвет: «{color_name}»{rgb_hint}. "
    elif color_code:
        color_part = f"Цвет: RGB({color_code}). "

    ref_note = (
        "Слайд конкурента предоставлен — используй его как референс стиля и композиции, "
        "не копируя бренд. " if has_competitor else ""
    )

    return (
        f'Создай 3 уникальных промта для внутреннего слайда карточки товара "{product}" ({paint_label}). '
        f"{color_part}"
        f"КРИТИЧЕСКИ ВАЖНО: упаковку (банку/тару) бери СТРОГО с предоставленного рендера ТОЧЬ-В-ТОЧЬ — форма, этикетка, цвет, пропорции без каких-либо изменений. "
        f"{ref_note}"
        f"Дизайнерский запрос (что показать на слайде): {design_req}. "
        f"Текст, который должен быть на слайде: {text_content}."
    )


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
    if paint_type == "lacquer":
        finish = _detect_lacquer_finish(product)
        if finish:
            points.append(
                f"{len(points) + 1}) Лак {finish} — поверхность на обложке визуально передаёт "
                f"эффект {finish} покрытия (строго {finish}, не путать с другими типами)."
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


async def _fresh_ref_urls(ref_file_ids: list[str]) -> list[str]:
    """Get fresh Telegram download URLs from permanent file_ids."""
    urls = []
    for fid in ref_file_ids:
        u = await _tg_url(fid)
        if u:
            urls.append(u)
    return urls


async def _send_image(
    target: Message, url: str, prompt: str, label: str,
    ref_file_ids: list[str] | None = None,
    meta: dict | None = None,
):
    image_id = uuid.uuid4().hex[:10]
    _image_store[image_id] = {
        "prompt": prompt, "url": url,
        "ref_file_ids": ref_file_ids or [],
        **(meta or {}),
    }
    has_line = bool(meta and meta.get("line"))
    caption = f"{label}\n\n<i>{prompt[:800]}</i>"
    try:
        await target.answer_photo(
            photo=url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_image_kb(image_id, has_line=has_line),
        )
    except Exception:
        await target.answer(f"{label}: фото готово, но не удалось отправить.")


# --- Main pipeline ---

async def run_pipeline(message: Message, data: dict):
    user_request = _build_request(data)
    photo_ids: list[str] = data.get("photo_ids", [])
    color_photo_ids: list[str] = data.get("color_photo_ids", [])
    paint_type: str = data.get("paint_type", "furniture")
    _pipe_meta = {
        "line": data.get("line") or "",
        "paint_type": paint_type,
        "color_code": data.get("color_code") or "",
        "color_name": data.get("color_name") or "",
    }

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

    ref_file_ids = photo_ids[:4]
    ref_urls = await _fresh_ref_urls(ref_file_ids)

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
                await _send_image(message, url, prompt, f"Вариант {idx}/10", ref_file_ids, meta=_pipe_meta)
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

    if done["ok"] == 0:
        await message.answer(
            "⚠️ Ни одно изображение не сгенерировалось.\n"
            "Возможные причины:\n"
            "• Недостаточно баланса на PiAPI\n"
            "• Сервис PiAPI временно недоступен\n"
            "• Неверный тип задачи (TASK_TYPE)\n\n"
            "Проверьте логи Railway для деталей.",
            reply_markup=AGAIN_KB,
        )
    else:
        try:
            await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)
        except Exception as e:
            logging.error("AGAIN_KB send error: %s", e)


# --- Multiply idea ---

@dp.callback_query(MultiplyCallback.filter())
async def multiply_idea(
    query: CallbackQuery,
    callback_data: MultiplyCallback,
    state: FSMContext,
):
    data = _image_store.get(callback_data.image_id)
    if not data:
        await query.answer("Данные не найдены — перезапустите генерацию.", show_alert=True)
        return

    await query.answer("Генерирую 3 похожих варианта…")
    status = await query.message.answer("Генерирую 3 похожих обложки…")
    prompt = data["prompt"]
    done = {"n": 0, "ok": 0}

    ref_file_ids = data.get("ref_file_ids") or []
    ref_urls = await _fresh_ref_urls(ref_file_ids)

    async def gen_and_send(idx: int):
        url = await piapi_client.generate_image(prompt, ref_urls or None)
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(query.message, url, prompt, f"Размножение {idx}/3", ref_file_ids)
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

    await state.update_data(
        multiply_prompt=prompt,
        multiply_ref_file_ids=ref_file_ids,
    )
    await state.set_state(MultiplyFeedbackForm.awaiting_feedback)
    await query.message.answer(
        "Банка на размноженных изображениях получилась правильной?",
        reply_markup=_kb("✅ Да, банка верная", "❌ Нет, банка изменилась"),
    )


MULTIPLY_FEEDBACK_YES = "✅ Да, банка верная"
MULTIPLY_FEEDBACK_NO = "❌ Нет, банка изменилась"


@dp.message(MultiplyFeedbackForm.awaiting_feedback, F.text == MULTIPLY_FEEDBACK_YES)
async def multiply_feedback_yes(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)


@dp.message(MultiplyFeedbackForm.awaiting_feedback, F.text == MULTIPLY_FEEDBACK_NO)
async def multiply_feedback_no(message: Message, state: FSMContext):
    await message.answer(
        "Прикрепите фото банки — пересгенерирую эти же обложки с правильной банкой:",
        reply_markup=RESTART_KB,
    )
    await state.set_state(MultiplyFeedbackForm.awaiting_new_ref)


@dp.message(MultiplyFeedbackForm.awaiting_new_ref, F.photo)
async def multiply_new_ref(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt = data.get("multiply_prompt", "")
    new_ref_fid = message.photo[-1].file_id
    new_ref_url = await _tg_url(new_ref_fid)
    await state.clear()

    status = await message.answer("Генерирую 3 обложки с правильной банкой…")
    done = {"n": 0, "ok": 0}

    async def gen_and_send(idx: int):
        url = await piapi_client.generate_image(prompt, [new_ref_url] if new_ref_url else None)
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(message, url, prompt, f"Исправленное {idx}/3", [new_ref_fid])
        else:
            await message.answer(f"Исправленное {idx}: генерация не удалась.")
        try:
            await status.edit_text(f"Обработано {done['n']}/3 | Готово: {done['ok']}")
        except Exception:
            pass

    await asyncio.gather(*[gen_and_send(i + 1) for i in range(3)])

    try:
        await status.edit_text(f"Готово! {done['ok']}/3 обложек с правильной банкой.")
    except Exception:
        pass
    await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)


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
    await run_fix_pipeline(message, image_data, message.text.strip(), extra_ref_file_id=None)


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
    await run_fix_pipeline(message, image_data, correction, extra_ref_file_id=message.photo[-1].file_id)


async def run_fix_pipeline(
    message: Message,
    image_data: dict,
    correction: str,
    extra_ref_file_id: str | None,
):
    original_url = image_data["url"]
    ref_file_ids: list[str] = image_data.get("ref_file_ids", [])

    fix_prompt = (
        f"Возьми изображение как основу и внеси следующие исправления: {correction}. "
        f"Сохрани общую композицию, стиль и расположение остальных элементов без изменений. "
        f"Вертикальный формат 3:4, современный UX/UI дизайн, "
        f"высококачественная коммерческая обложка для маркетплейса."
    )

    # Fresh URLs: original generated image + fresh Telegram ref + extra ref photo
    image_urls = [original_url]
    fresh_refs = await _fresh_ref_urls(ref_file_ids)
    image_urls.extend(fresh_refs)
    if extra_ref_file_id:
        extra_url = await _tg_url(extra_ref_file_id)
        if extra_url:
            image_urls.append(extra_url)

    status = await message.answer("Исправляю изображение…")
    url = await piapi_client.generate_image(fix_prompt, image_urls)
    if url:
        await _send_image(message, url, fix_prompt, "Исправленный вариант", ref_file_ids)
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer("Хотите сделать ещё одну серию?", reply_markup=AGAIN_KB)
    else:
        await status.edit_text("Не удалось исправить изображение. Попробуйте ещё раз.")


# --- Scale to line colors ---

@dp.callback_query(ScaleColorsCB.filter(F.action == "start"))
async def handle_scale_start(query: CallbackQuery, callback_data: ScaleColorsCB):
    image_id = callback_data.image_id
    meta = _image_store.get(image_id, {})
    line = meta.get("line", "")
    if not line:
        await query.answer("Линейка не определена для этого товара", show_alert=True)
        return

    try:
        products = await sheets_client.load_products()
    except Exception:
        await query.answer("Ошибка загрузки базы данных", show_alert=True)
        return

    current_code = meta.get("color_code", "")
    seen_codes: set[str] = {current_code} if current_code else set()
    same_line: list[dict] = []
    for p in products:
        if p.get("line") != line:
            continue
        code = p.get("rgb", "")
        if code in seen_codes:
            continue
        seen_codes.add(code)
        same_line.append(p)

    if not same_line:
        await query.answer("Других цветов в этой линейке не найдено в базе", show_alert=True)
        return

    _scale_data[image_id] = {"products": same_line, "selected": set()}
    await query.answer()
    await query.message.answer(
        f"🎨 Линейка «{line}» — найдено {len(same_line)} других цвет(а/ов).\n"
        "Отметьте нужные и нажмите «Генерировать»:",
        reply_markup=_build_scale_kb(image_id, same_line, set()),
    )


@dp.callback_query(ScaleColorsCB.filter(F.action == "toggle"))
async def handle_scale_toggle(query: CallbackQuery, callback_data: ScaleColorsCB):
    image_id = callback_data.image_id
    idx = callback_data.idx
    sd = _scale_data.get(image_id)
    if sd is None:
        await query.answer("Сессия устарела — нажмите «Цвета линейки» снова", show_alert=True)
        return
    if idx in sd["selected"]:
        sd["selected"].discard(idx)
    else:
        sd["selected"].add(idx)
    await query.message.edit_reply_markup(
        reply_markup=_build_scale_kb(image_id, sd["products"], sd["selected"])
    )
    await query.answer()


@dp.callback_query(ScaleColorsCB.filter(F.action == "cancel"))
async def handle_scale_cancel(query: CallbackQuery, callback_data: ScaleColorsCB):
    _scale_data.pop(callback_data.image_id, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer("Отменено")


@dp.callback_query(ScaleColorsCB.filter(F.action == "confirm"))
async def handle_scale_confirm(query: CallbackQuery, callback_data: ScaleColorsCB):
    image_id = callback_data.image_id
    sd = _scale_data.pop(image_id, None)
    if sd is None:
        await query.answer("Сессия устарела — нажмите «Цвета линейки» снова", show_alert=True)
        return

    selected_idxs = sd["selected"]
    all_products = sd["products"]
    target_products = [all_products[i] for i in sorted(selected_idxs)] if selected_idxs else all_products

    meta = _image_store.get(image_id, {})
    original_prompt = meta.get("prompt", "")
    ref_file_ids = meta.get("ref_file_ids", [])
    old_code = meta.get("color_code", "")
    old_name = meta.get("color_name", "")
    line = meta.get("line", "")
    paint_type = meta.get("paint_type", "furniture")

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer()

    status = await query.message.answer(
        f"🎨 Масштабирую на {len(target_products)} цвет(а)...\nЭто займёт несколько минут."
    )

    ref_urls = await _fresh_ref_urls(ref_file_ids)
    done = {"n": 0, "ok": 0}

    async def gen_color(product: dict):
        new_code = product.get("rgb", "")
        new_name = product.get("color_name", "")
        new_prompt = _swap_color_in_prompt(original_prompt, old_code, old_name, new_code, new_name)
        url = await piapi_client.generate_image(new_prompt, ref_urls or None)
        done["n"] += 1
        label_color = new_name or new_code or "?"
        if url:
            done["ok"] += 1
            await _send_image(
                query.message, url, new_prompt, f"🎨 {label_color}",
                ref_file_ids,
                meta={"line": line, "paint_type": paint_type, "color_code": new_code, "color_name": new_name},
            )
        else:
            await query.message.answer(f"❌ Не удалось: {label_color}")
        try:
            await status.edit_text(f"🎨 Масштабирую... {done['n']}/{len(target_products)} готово")
        except Exception:
            pass

    await asyncio.gather(*[gen_color(p) for p in target_products])

    try:
        await status.edit_text(f"✅ Готово! {done['ok']}/{len(target_products)} обложек сгенерировано.")
    except Exception:
        pass


# =====================================================================
# === ВНУТРЕННИЕ СЛАЙДЫ ===
# =====================================================================

def _build_slide_search_kb(results: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(results):
        rows.append([InlineKeyboardButton(
            text=_product_btn_text(p),
            callback_data=SlideProductSelectCallback(idx=i).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(SlideForm.type_select, F.text == "📎 С референса конкурента")
async def slide_type_reference(message: Message, state: FSMContext):
    await state.update_data(slide_with_reference=True)
    await message.answer(
        "Отправьте <b>фото внутреннего слайда конкурента</b> — он станет референсом стиля:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.competitor_slide)


@dp.message(SlideForm.type_select, F.text == "✨ С ноля")
async def slide_type_scratch(message: Message, state: FSMContext):
    await state.update_data(slide_with_reference=False)
    await message.answer(
        "Введите название товара или линейки для поиска в базе:",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.product_search)


@dp.message(SlideForm.type_select)
async def slide_type_bad(message: Message):
    await message.answer("Выберите вариант с помощью кнопок:", reply_markup=SLIDE_TYPE_KB)


@dp.message(SlideForm.competitor_slide, F.photo)
async def slide_competitor_photo(message: Message, state: FSMContext):
    await state.update_data(slide_competitor_fid=message.photo[-1].file_id)
    await message.answer(
        "Хорошо! Теперь введите название товара или линейки для поиска в базе:",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.product_search)


@dp.message(SlideForm.competitor_slide)
async def slide_competitor_bad(message: Message):
    await message.answer("Отправьте фото слайда конкурента:", reply_markup=BACK_RESTART_KB)


@dp.message(SlideForm.product_search, F.text)
async def slide_product_search(message: Message, state: FSMContext):
    query = message.text.strip()
    status = await message.answer("🔍 Ищу в базе…")
    try:
        products = await sheets_client.load_products()
    except Exception as e:
        await status.edit_text(f"❌ Не удалось загрузить базу: {e}")
        return
    results = sheets_client.search_products(query, products)
    if not results:
        await status.edit_text(f"Ничего не найдено по «{query}». Попробуйте другой запрос.")
        return
    if len(results) > 10:
        await status.edit_text(
            f"Найдено {len(results)} позиций — слишком много. Уточните запрос."
        )
        return
    await state.update_data(slide_search_results=results)
    await status.edit_text(
        f"Найдено {len(results)} позиций. Выберите товар:",
        reply_markup=_build_slide_search_kb(results),
    )


@dp.callback_query(SlideProductSelectCallback.filter(), SlideForm.product_search)
async def slide_product_select(
    query: CallbackQuery,
    callback_data: SlideProductSelectCallback,
    state: FSMContext,
):
    await query.answer()
    data = await state.get_data()
    results: list[dict] = data.get("slide_search_results", [])
    idx = callback_data.idx
    if idx >= len(results):
        await query.message.answer("Ошибка выбора, попробуйте снова.", reply_markup=BACK_RESTART_KB)
        return
    product = results[idx]
    await state.update_data(
        product_name=product["name"],
        paint_type=product["paint_type"],
        color_code=product["rgb"] if product["rgb"] else None,
        color_name=product["color_name"],
        line=product["line"],
    )
    color_info = f"\n<b>Цвет:</b> {product['color_name']}" if product["color_name"] else ""
    await query.message.answer(
        f"✅ <b>{product['name']}</b>{color_info}",
        parse_mode="HTML",
    )
    await query.message.answer(
        "Отправьте <b>рендер банки краски</b> — чистое изображение упаковки:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.render_photo)


@dp.message(SlideForm.render_photo, F.photo)
async def slide_render_photo(message: Message, state: FSMContext):
    await state.update_data(slide_render_fid=message.photo[-1].file_id)
    await message.answer(
        "Что нужно <b>показать</b> на слайде?\n"
        "<i>Пример: процесс нанесения, поверхность до/после, крупный план банки, схема применения</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.design_request)


@dp.message(SlideForm.render_photo)
async def slide_render_bad(message: Message):
    await message.answer("Отправьте фото рендера банки:", reply_markup=BACK_RESTART_KB)


@dp.message(SlideForm.design_request, F.text)
async def slide_design_request(message: Message, state: FSMContext):
    await state.update_data(slide_design_request=message.text.strip())
    await message.answer(
        "Что нужно <b>написать</b> на слайде?\n"
        "<i>Пример: «Экономичный расход 12 м²/л», «Не требует грунтовки», список характеристик</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(SlideForm.text_content)


@dp.message(SlideForm.text_content, F.text)
async def slide_text_content(message: Message, state: FSMContext):
    await state.update_data(slide_text_content=message.text.strip())
    data = await state.get_data()
    await state.clear()
    await message.answer("Принято! Генерирую слайды…", reply_markup=ReplyKeyboardRemove())
    await run_slide_pipeline(message, state, data)


async def run_slide_pipeline(message: Message, state: FSMContext, data: dict):
    user_request = _build_slide_request(data)

    status = await message.answer("Генерирую промты для слайдов…")

    try:
        prompts = await claude_client.generate_slide_prompts(user_request, data.get("paint_type", "furniture"))
    except Exception as e:
        await status.edit_text(f"Ошибка генерации промтов: {e}")
        await message.answer("Попробуйте ещё раз:", reply_markup=SLIDE_POST_GEN_KB)
        return

    await status.edit_text(f"{len(prompts)} промта готово! Отправляю в Nano Banana Pro…")

    render_fid = data.get("slide_render_fid")
    competitor_fid = data.get("slide_competitor_fid")

    ref_fids = []
    if competitor_fid:
        ref_fids.append(competitor_fid)
    if render_fid:
        ref_fids.append(render_fid)

    ref_urls = await _fresh_ref_urls(ref_fids) if ref_fids else []

    done = {"n": 0, "ok": 0}

    async def gen_slide(idx: int, prompt: str):
        url = await piapi_client.generate_image(prompt, ref_urls or None)
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(message, url, prompt, f"Слайд {idx}/{len(prompts)}", ref_fids)
        else:
            await message.answer(f"Слайд {idx}: генерация не удалась.")
        try:
            await status.edit_text(f"Обработано {done['n']}/{len(prompts)} | Готово: {done['ok']}")
        except Exception:
            pass

    await asyncio.gather(*[gen_slide(i + 1, p) for i, p in enumerate(prompts)])

    try:
        await status.edit_text(f"Готово! Сгенерировано {done['ok']}/{len(prompts)} слайдов.")
    except Exception:
        pass

    # Сохраняем данные для повторной генерации
    await state.update_data(**data)
    await state.set_state(SlideForm.post_gen)

    if done["ok"] == 0:
        await message.answer(
            "⚠️ Ни один слайд не сгенерировался. Проверьте баланс PiAPI.",
            reply_markup=SLIDE_POST_GEN_KB,
        )
    else:
        await message.answer("Что дальше?", reply_markup=SLIDE_POST_GEN_KB)


@dp.message(SlideForm.post_gen, F.text == "🔄 Ещё такие же")
async def slide_post_again(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("Генерирую ещё раз…", reply_markup=ReplyKeyboardRemove())
    await run_slide_pipeline(message, state, data)


@dp.message(SlideForm.post_gen, F.text == "✏️ Изменить дизайнерский запрос")
async def slide_post_change_design(message: Message, state: FSMContext):
    await message.answer("Введите новый дизайнерский запрос:", reply_markup=BACK_RESTART_KB)
    await state.set_state(SlideForm.new_design_request)


@dp.message(SlideForm.new_design_request, F.text)
async def slide_new_design_request(message: Message, state: FSMContext):
    await state.update_data(slide_design_request=message.text.strip())
    data = await state.get_data()
    await state.clear()
    await message.answer("Генерирую с новым запросом…", reply_markup=ReplyKeyboardRemove())
    await run_slide_pipeline(message, state, data)


# =====================================================================
# === БАННЕРЫ ===
# =====================================================================

def _build_banner_search_kb(results: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(results):
        rows.append([InlineKeyboardButton(
            text=_product_btn_text(p),
            callback_data=BannerProductSelectCallback(idx=i).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_banner_request(data: dict) -> str:
    product = data.get("product_name", "")
    paint_type = data.get("paint_type", "furniture")
    color_name = data.get("color_name", "")
    color_code = data.get("color_code", "")
    design_req = data.get("banner_design_request", "")
    headline = data.get("banner_headline", "")
    subtitle = data.get("banner_subtitle", "")

    paint_labels = {
        "walls": "краска для стен", "lacquer": "лак для мебели",
        "primer": "грунтовка для мебели", "furniture": "краска для мебели",
    }
    paint_label = paint_labels.get(paint_type, paint_type)

    color_part = ""
    if color_name:
        rgb_hint = f" (точный RGB: {color_code})" if color_code else ""
        color_part = f"Цвет: «{color_name}»{rgb_hint}. "
    elif color_code:
        color_part = f"Цвет: RGB({color_code}). "

    return (
        f'Создай 3 уникальных промта для рекламного баннера товара "{product}" ({paint_label}). '
        f"{color_part}"
        f"КРИТИЧЕСКИ ВАЖНО: банку бери СТРОГО с предоставленного рендера ТОЧЬ-В-ТОЧЬ — "
        f"форма, этикетка, цвет, пропорции без изменений. "
        f"Что показать на баннере: {design_req}. "
        f'Большой заголовок (огромный жирный текст): «{headline}». '
        f'Мелкий подзаголовок (небольшой текст под заголовком): «{subtitle}».'
    )


async def run_banner_pipeline(message: Message, state: FSMContext, data: dict):
    user_request = _build_banner_request(data)
    status = await message.answer("Генерирую промты для баннеров…")

    try:
        prompts = await claude_client.generate_banner_prompts(user_request)
    except Exception as e:
        await status.edit_text(f"Ошибка генерации промтов: {e}")
        await message.answer("Попробуйте ещё раз:", reply_markup=BANNER_POST_GEN_KB)
        return

    await status.edit_text(f"{len(prompts)} промта готово! Отправляю в Nano Banana Pro…")

    render_fid = data.get("banner_render_fid")
    ref_urls = await _fresh_ref_urls([render_fid]) if render_fid else []

    done = {"n": 0, "ok": 0}

    async def gen_banner(idx: int, prompt: str):
        url = await piapi_client.generate_image(prompt, ref_urls or None, aspect_ratio="16:9")
        done["n"] += 1
        if url:
            done["ok"] += 1
            await _send_image(message, url, prompt, f"Баннер {idx}/{len(prompts)}", [render_fid] if render_fid else [])
        else:
            await message.answer(f"Баннер {idx}: генерация не удалась.")
        try:
            await status.edit_text(f"Обработано {done['n']}/{len(prompts)} | Готово: {done['ok']}")
        except Exception:
            pass

    await asyncio.gather(*[gen_banner(i + 1, p) for i, p in enumerate(prompts)])

    try:
        await status.edit_text(f"Готово! Сгенерировано {done['ok']}/{len(prompts)} баннеров.")
    except Exception:
        pass

    await state.update_data(**data)
    await state.set_state(BannerForm.post_gen)

    if done["ok"] == 0:
        await message.answer(
            "⚠️ Ни один баннер не сгенерировался. Проверьте баланс PiAPI.",
            reply_markup=BANNER_POST_GEN_KB,
        )
    else:
        await message.answer("Что дальше?", reply_markup=BANNER_POST_GEN_KB)


@dp.message(BannerForm.product_search, F.text)
async def banner_product_search(message: Message, state: FSMContext):
    query = message.text.strip()
    status = await message.answer("🔍 Ищу в базе…")
    try:
        products = await sheets_client.load_products()
    except Exception as e:
        await status.edit_text(f"❌ Не удалось загрузить базу: {e}")
        return
    results = sheets_client.search_products(query, products)
    if not results:
        await status.edit_text(f"Ничего не найдено по «{query}». Попробуйте другой запрос.")
        return
    if len(results) > 10:
        await status.edit_text(
            f"Найдено {len(results)} позиций — слишком много. Уточните запрос."
        )
        return
    await state.update_data(banner_search_results=results)
    await status.edit_text(
        f"Найдено {len(results)} позиций. Выберите товар:",
        reply_markup=_build_banner_search_kb(results),
    )


@dp.callback_query(BannerProductSelectCallback.filter(), BannerForm.product_search)
async def banner_product_select(
    query: CallbackQuery,
    callback_data: BannerProductSelectCallback,
    state: FSMContext,
):
    await query.answer()
    data = await state.get_data()
    results: list[dict] = data.get("banner_search_results", [])
    idx = callback_data.idx
    if idx >= len(results):
        await query.message.answer("Ошибка выбора, попробуйте снова.", reply_markup=BACK_RESTART_KB)
        return
    product = results[idx]
    await state.update_data(
        product_name=product["name"],
        paint_type=product["paint_type"],
        color_code=product["rgb"] if product["rgb"] else None,
        color_name=product["color_name"],
        line=product["line"],
    )
    color_info = f"\n<b>Цвет:</b> {product['color_name']}" if product["color_name"] else ""
    await query.message.answer(
        f"✅ <b>{product['name']}</b>{color_info}",
        parse_mode="HTML",
    )
    await query.message.answer(
        "Отправьте <b>рендер банки краски</b> — чистое изображение упаковки:",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(BannerForm.render_photo)


@dp.message(BannerForm.render_photo, F.photo)
async def banner_render_photo(message: Message, state: FSMContext):
    await state.update_data(banner_render_fid=message.photo[-1].file_id)
    await message.answer(
        "Что нужно <b>показать</b> на баннере?\n"
        "<i>Пример: банка на фоне выкраски, рука с кистью, покрашенная поверхность, lifestyle</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(BannerForm.design_request)


@dp.message(BannerForm.render_photo)
async def banner_render_bad(message: Message):
    await message.answer("Отправьте фото рендера банки:", reply_markup=BACK_RESTART_KB)


@dp.message(BannerForm.design_request, F.text)
async def banner_design_request(message: Message, state: FSMContext):
    await state.update_data(banner_design_request=message.text.strip())
    await message.answer(
        "Введите <b>большой заголовок</b> баннера:\n"
        "<i>Пример: «Матовая краска», «Та самая краска»</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(BannerForm.headline)


@dp.message(BannerForm.headline, F.text)
async def banner_headline(message: Message, state: FSMContext):
    await state.update_data(banner_headline=message.text.strip())
    await message.answer(
        "Введите <b>мелкий подзаголовок</b> баннера:\n"
        "<i>Пример: «Для мебели и декора», «Премиальная матовая краска»</i>",
        parse_mode="HTML",
        reply_markup=BACK_RESTART_KB,
    )
    await state.set_state(BannerForm.subtitle)


@dp.message(BannerForm.subtitle, F.text)
async def banner_subtitle(message: Message, state: FSMContext):
    await state.update_data(banner_subtitle=message.text.strip())
    data = await state.get_data()
    await state.clear()
    await message.answer("Принято! Генерирую баннеры…", reply_markup=ReplyKeyboardRemove())
    await run_banner_pipeline(message, state, data)


@dp.message(BannerForm.post_gen, F.text == "🔄 Ещё такие же баннеры")
async def banner_post_again(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("Генерирую ещё раз…", reply_markup=ReplyKeyboardRemove())
    await run_banner_pipeline(message, state, data)


@dp.message(BannerForm.post_gen, F.text == "✏️ Изменить дизайнерский запрос")
async def banner_post_change_design(message: Message, state: FSMContext):
    await message.answer("Введите новый дизайнерский запрос:", reply_markup=BACK_RESTART_KB)
    await state.set_state(BannerForm.new_design_request)


@dp.message(BannerForm.new_design_request, F.text)
async def banner_new_design_request(message: Message, state: FSMContext):
    await state.update_data(banner_design_request=message.text.strip())
    data = await state.get_data()
    await state.clear()
    await message.answer("Генерирую с новым запросом…", reply_markup=ReplyKeyboardRemove())
    await run_banner_pipeline(message, state, data)


# Catch-all: если состояние сброшено (редеплой) — отправляем в начало
@dp.message()
async def catch_all(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await _start_form(message, state)


async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
