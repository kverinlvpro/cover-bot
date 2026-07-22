import csv
import logging
import time
from io import StringIO

import httpx

logger = logging.getLogger(__name__)

SHEET_ID = "1fRBfWskkVLkxhZ9l3lj7eoWyelxG4joAIJbUYz_9fkw"
_SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    "/gviz/tq?tqx=out:csv"
)

_cache: list[dict] = []
_cache_time: float = 0
_CACHE_TTL = 300  # 5 минут


def _parse_rgb(raw: str) -> str:
    raw = raw.strip()
    if raw.lower().startswith("rgb("):
        raw = raw[4:].rstrip(")")
    return raw.strip()


def _get_paint_type(category: str) -> str:
    cat = category.lower()
    if any(w in cat for w in ("стен", "потолок", "фасад", "интерьер", "обои")):
        return "walls"
    return "furniture"


async def load_products(force: bool = False) -> list[dict]:
    global _cache, _cache_time
    if not force and _cache and time.time() - _cache_time < _CACHE_TTL:
        return _cache

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as http:
            r = await http.get(_SHEET_URL)
            r.raise_for_status()
    except Exception as e:
        logger.error("sheets load error: %s", e)
        if _cache:
            logger.warning("returning stale cache")
            return _cache
        raise

    products = []
    reader = csv.DictReader(StringIO(r.text))
    for row in reader:
        name = row.get("Название", "").strip()
        if not name:
            continue
        rgb_raw = row.get("Код цвета", "").strip()
        utps_raw = row.get("УТП", "").strip()
        products.append({
            "name": name,
            "artikul": row.get("Артикул", "").strip(),
            "category": row.get("Категория", "").strip(),
            "line": row.get("Линейка", "").strip(),
            "volume": row.get("Фасовка для МП", "").strip(),
            "rgb": _parse_rgb(rgb_raw) if rgb_raw else "",
            "color_name": row.get("Цвет", "").strip(),
            "utps": [u.strip() for u in utps_raw.split(",") if u.strip()],
            "paint_type": _get_paint_type(row.get("Категория", "")),
        })

    _cache = products
    _cache_time = time.time()
    logger.info("sheets: loaded %d products", len(products))
    return products


def search_products(query: str, products: list[dict]) -> list[dict]:
    q = query.lower().strip()
    if not q:
        return []
    return [
        p for p in products
        if q in p["name"].lower()
        or q in p["line"].lower()
        or q in p["artikul"].lower()
    ]
