from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import statistics
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import yaml
from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Response, TimeoutError as PlaywrightTimeoutError, async_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "state.json"
DEBUG_DIR = ROOT / "debug"

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOG = logging.getLogger("wallapop-watch")


@dataclass
class Discovery:
    url: str
    title: str = ""
    text: str = ""
    price: float | None = None
    image: str | None = None
    query: str = ""
    source: str = "dom"


@dataclass
class Listing:
    item_id: str
    url: str
    title: str
    description: str
    price: float
    brand: str | None
    size: int
    condition: str
    city: str
    image: str | None
    listing_age_hours: float | None
    recency_text: str
    seller_name: str
    seller_profile_url: str | None
    seller_active_hours: float | None
    seller_activity_text: str
    active: bool
    purchasable: bool
    is_exotic: bool
    query: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"initialized": False, "seen": {}, "samples": [], "price_recheck_cursor": 0, "last_run": None}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        LOG.warning("State file was unreadable; starting with an empty state.")
        return {"initialized": False, "seen": {}, "samples": [], "price_recheck_cursor": 0, "last_run": None}
    state.setdefault("initialized", False)
    state.setdefault("seen", {})
    state.setdefault("samples", [])
    state.setdefault("price_recheck_cursor", 0)
    state.setdefault("last_run", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    temp.replace(STATE_PATH)


def strip_accents(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))


def normalize(value: Any) -> str:
    text = strip_accents(str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def nested_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        clean = value.replace("€", "").replace("EUR", "").strip()
        clean = re.sub(r"[^0-9,\.]", "", clean)
        if not clean:
            return None
        if "," in clean and "." in clean:
            clean = clean.replace(".", "").replace(",", ".")
        elif "," in clean:
            clean = clean.replace(",", ".")
        try:
            return float(clean)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("amount", "price", "value", "cash", "total"):
            result = nested_price(value.get(key))
            if result is not None:
                return result
    return None


def extract_price_from_text(text: str) -> float | None:
    matches = re.findall(r"(?<!\d)(\d{1,4}(?:[\.,]\d{1,2})?)\s*€", text)
    for match in matches:
        value = nested_price(match)
        if value is not None and 1 <= value <= 10000:
            return value
    return None


def canonical_url(value: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("/"):
        value = urljoin("https://es.wallapop.com", value)
    elif value.startswith("item/"):
        value = "https://es.wallapop.com/" + value
    elif not value.startswith("http") and "-" in value:
        value = "https://es.wallapop.com/item/" + value.strip("/")
    if "/item/" not in value:
        return None
    parsed = urlparse(value)
    return f"https://es.wallapop.com{parsed.path}".rstrip("/")


def item_id_from_url(url: str) -> str:
    match = re.search(r"-(\d+)$", url.rstrip("/"))
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def extract_size(text: str, allowed: set[int]) -> int | None:
    patterns = [
        r"(?:talla|size|numero|número|num\.?|nº|eu)\s*[:#\-]?\s*(4[0-5])(?:\b|\s)",
        r"\b(4[0-5])\s*(?:eu|europea|europeo)\b",
        r"\b(4[0-5])\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = int(match.group(1))
            if value in allowed:
                return value
    return None


def detect_brand(text: str, aliases: dict[str, list[str]]) -> str | None:
    haystack = f" {normalize(text)} "
    # Longer aliases first, so "san diego boots" wins before a shorter accidental match.
    candidates: list[tuple[str, str]] = []
    for canonical, variants in aliases.items():
        for variant in variants:
            candidates.append((canonical, normalize(variant)))
    for canonical, variant in sorted(candidates, key=lambda pair: len(pair[1]), reverse=True):
        if f" {variant} " in haystack or variant in haystack:
            return canonical
    return None


def contains_any(text: str, values: Iterable[str]) -> bool:
    haystack = normalize(text)
    return any(normalize(value) in haystack for value in values)


def looks_relevant(text: str, cfg: dict[str, Any]) -> bool:
    brand = detect_brand(text, cfg["brand_aliases"])
    has_boot_term = contains_any(text, cfg["boot_terms"])
    has_allowed_size = extract_size(text, set(cfg["settings"]["sizes"])) is not None
    has_non_boot_collision = contains_any(text, cfg.get("non_boot_terms", []))
    normalized_text = normalize(text)
    leading_non_boot = any(
        normalized_text.startswith(normalize(term) + " ")
        or normalized_text.startswith(normalize(term) + ":")
        for term in cfg.get("non_boot_terms", [])
    )

    # Recommendation cards and long descriptions can contain words such as
    # “libro”, “figura” or “CD”. Do not reject a clearly branded boot because
    # of unrelated page noise. But reject pages whose actual headline begins
    # with a known non-footwear category, such as a book title.
    if leading_non_boot or (has_non_boot_collision and brand is None):
        return False

    return has_boot_term or (brand is not None and has_allowed_size)


def primary_product_text(body: str, title: str = "") -> str:
    """Extract the current listing block without recommendation-card pollution.

    Wallapop can place navigation and recommendation text before or after the
    listing details. Start near the listing title when possible, then stop at a
    known recommendation heading.
    """
    stop_markers = {
        "productos similares",
        "tambien te puede interesar",
        "también te puede interesar",
        "otros productos",
        "mas productos",
        "más productos",
        "anuncios relacionados",
        "recomendaciones para ti",
    }
    lines = [raw.strip() for raw in body.splitlines() if raw.strip()]
    if not lines:
        return ""

    start_index = 0
    normalized_title = normalize(title)
    if normalized_title:
        for index, line in enumerate(lines):
            line_norm = normalize(line)
            if normalized_title in line_norm or line_norm in normalized_title:
                start_index = max(0, index - 3)
                break

    marker_norms = {normalize(marker) for marker in stop_markers}
    kept: list[str] = []
    for line in lines[start_index:]:
        # Do not let a recommendation heading before the real title create an
        # empty block. Only stop after some listing content has been collected.
        if kept and normalize(line) in marker_norms:
            break
        kept.append(line)
        if len(kept) >= 180 or sum(len(value) for value in kept) >= 9000:
            break
    return "\n".join(kept)


def discovery_maybe_relevant(item: Discovery, cfg: dict[str, Any]) -> bool:
    title = item.title or ""
    text = f"{title}\n{item.text}"
    if contains_any(text, cfg["excluded_terms"]):
        return False
    if contains_any(text, cfg.get("non_boot_terms", [])):
        return False

    title_brand = detect_brand(title, cfg["brand_aliases"])
    card_brand = detect_brand(text, cfg["brand_aliases"])
    query_brand = detect_brand(item.query, cfg["brand_aliases"])
    title_has_boot = contains_any(title, cfg["boot_terms"])
    card_has_boot = contains_any(text, cfg["boot_terms"])
    has_allowed_size = extract_size(text, set(cfg["settings"]["sizes"])) is not None

    # Strong card evidence: the title itself says boot/boot brand.
    if title_has_boot or title_brand is not None:
        return True

    # For a brand search, accept a matching branded card when size/footwear
    # evidence exists in the card body. This catches terse titles.
    if query_brand is not None and card_brand == query_brand and (card_has_boot or has_allowed_size):
        return True

    # Generic queries need explicit footwear evidence, not just a word collision.
    return card_has_boot and has_allowed_size


def parse_relative_hours(fragment: str) -> float | None:
    text = normalize(fragment)
    if not text:
        return None
    if "ahora" in text or "unos segundos" in text:
        return 0.02
    if "ayer" in text:
        return 24.0
    if "menos de una hora" in text or "menos de 1 hora" in text:
        return 0.5
    if "una hora" in text or "1 hora" in text:
        return 1.0
    if "un minuto" in text or "1 minuto" in text:
        return 1 / 60
    if "un dia" in text or "1 dia" in text:
        return 24.0
    if "una semana" in text or "1 semana" in text:
        return 168.0
    number_match = re.search(r"(\d+)", text)
    if not number_match:
        return None
    number = int(number_match.group(1))
    if "min" in text:
        return number / 60
    if "hora" in text:
        return float(number)
    if "dia" in text:
        return float(number * 24)
    if "semana" in text:
        return float(number * 168)
    if "mes" in text:
        return float(number * 24 * 30)
    if "ano" in text:
        return float(number * 24 * 365)
    return None


def extract_listing_recency(body: str) -> tuple[float | None, str]:
    normalized = normalize(body)
    patterns = [
        r"((?:publicado|editado|subido|actualizado)\s+hace\s+(?:menos de\s+)?(?:una|un|\d+)\s+(?:minuto|minutos|hora|horas|dia|dias|semana|semanas|mes|meses|ano|anos))",
        r"((?:publicado|editado|subido|actualizado)\s+ayer)",
        r"((?:publicado|editado|subido|actualizado)\s+ahora)",
    ]
    found: list[tuple[float, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            phrase = match.group(1)
            hours = parse_relative_hours(phrase)
            if hours is not None:
                found.append((hours, phrase))
    if not found:
        # Numeric absolute dates occasionally appear in the listing page.
        for match in re.finditer(r"(?:publicado|editado|actualizado)\s+(?:el\s+)?(\d{1,2})[\-/](\d{1,2})[\-/](\d{4})", normalized):
            day, month, year = map(int, match.groups())
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
                hours = max(0.0, (utc_now() - dt).total_seconds() / 3600)
                found.append((hours, match.group(0)))
            except ValueError:
                continue
    if not found:
        return None, "unverified"
    # Use the freshest publication/edit evidence visible.
    hours, phrase = min(found, key=lambda pair: pair[0])
    return hours, phrase


def extract_seller_activity(body: str) -> tuple[float | None, str]:
    normalized = normalize(body)
    patterns = [
        r"((?:activo|activa|conectado|conectada|ultima conexion|ultima vez)\s+(?:hace\s+)?(?:menos de\s+)?(?:una|un|\d+)\s+(?:minuto|minutos|hora|horas|dia|dias|semana|semanas|mes|meses|ano|anos))",
        r"((?:activo|activa|conectado|conectada)\s+ahora)",
        r"((?:activo|activa|conectado|conectada)\s+ayer)",
    ]
    matches: list[tuple[float, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            phrase = match.group(1)
            hours = parse_relative_hours(phrase)
            if hours is not None:
                matches.append((hours, phrase))
    if not matches:
        return None, "unverified"
    return min(matches, key=lambda pair: pair[0])


def extract_condition(body: str) -> str:
    normalized = normalize(body)
    conditions = [
        ("Nuevo", ["nuevo sin etiquetas", "nuevo con etiquetas", "sin estrenar", "nuevo"]),
        ("Como nuevo", ["como nuevo", "practicamente nuevo", "prácticamente nuevo"]),
        ("Buen estado", ["buen estado", "muy buen estado"]),
        ("Used", ["usado", "con uso", "aceptable"]),
    ]
    for label, phrases in conditions:
        if any(normalize(phrase) in normalized for phrase in phrases):
            return label
    return "Not clearly stated"


def extract_city(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if normalize(line) in {"ubicacion", "ubicación", "localizacion", "localización"} and index + 1 < len(lines):
            return lines[index + 1][:80]
    # Common Wallapop detail ordering: recency, views, city. Keep this conservative.
    for line in lines:
        if re.fullmatch(r"[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{2,45}", line) and line not in {"Wallapop", "España"}:
            if not contains_any(line, ["Nuevo", "Como nuevo", "Buen estado", "Comprar", "Chat"]):
                return line
    return "Unknown"


def extract_jsonld_product(raw_scripts: list[str]) -> dict[str, Any]:
    products: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            type_value = value.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any(str(t).lower() == "product" for t in types if t):
                products.append(value)
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for raw in raw_scripts:
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return products[0] if products else {}


def availability_is_active(product: dict[str, Any]) -> bool | None:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None
    availability = normalize(offers.get("availability"))
    if not availability:
        return None
    if "instock" in availability or "in stock" in availability:
        return True
    if any(value in availability for value in ("outofstock", "soldout", "discontinued")):
        return False
    return None


def product_price(product: dict[str, Any]) -> float | None:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        value = nested_price(first(offers, "price", "lowPrice", "highPrice"))
        if value is not None:
            return value
    return nested_price(product.get("price"))


def product_image(product: dict[str, Any]) -> str | None:
    image = product.get("image")
    if isinstance(image, list):
        return str(image[0]) if image else None
    if isinstance(image, dict):
        return str(first(image, "url", "contentUrl", default="")) or None
    return str(image) if image else None


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from walk_json(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_json(nested)


def discovery_from_object(obj: dict[str, Any], query: str) -> Discovery | None:
    title = str(first(obj, "title", "name", "headline", default=""))
    description = str(first(obj, "description", "body", "text", default=""))
    price = nested_price(first(obj, "price", "sale_price", "salePrice", "amount"))
    url_value = str(first(obj, "web_url", "webUrl", "share_url", "shareUrl", "url", "web_slug", "webSlug", "slug", default=""))
    url = canonical_url(url_value)
    if not url and first(obj, "id", "item_id", "itemId") and first(obj, "web_slug", "webSlug", "slug"):
        url = canonical_url(str(first(obj, "web_slug", "webSlug", "slug")))
    if not url or not title or price is None:
        return None
    image_value = first(obj, "image", "images", "picture", "pictures", default=None)
    image: str | None = None
    if isinstance(image_value, str):
        image = image_value
    elif isinstance(image_value, list) and image_value:
        first_image = image_value[0]
        image = str(first_image) if isinstance(first_image, str) else str(first(first_image, "url", "original", default="")) if isinstance(first_image, dict) else None
    elif isinstance(image_value, dict):
        image = str(first(image_value, "url", "original", default="")) or None
    return Discovery(url=url, title=title, text=f"{title}\n{description}", price=price, image=image, query=query, source="json")


async def accept_cookies(page: Page) -> None:
    labels = ["Aceptar y continuar", "Aceptar todo", "Aceptar", "Allow all", "Accept all"]
    for label in labels:
        locator = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE))
        try:
            if await locator.count() and await locator.first.is_visible(timeout=500):
                await locator.first.click(timeout=1500)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def save_debug(page: Page, name: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:90]
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{timestamp}_{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{timestamp}_{safe}.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass


def blocked_page(body: str, title: str) -> bool:
    text = normalize(f"{title} {body}")
    phrases = [
        "access denied",
        "acceso denegado",
        "forbidden",
        "error 403",
        "captcha",
        "verify you are human",
        "verifica que eres humano",
        "too many requests",
        "demasiadas solicitudes",
    ]
    return any(phrase in text for phrase in phrases)


async def collect_dom_discoveries(page: Page, query: str) -> list[Discovery]:
    records = await page.locator('a[href*="/item/"]').evaluate_all(
        """
        (links) => links.map((a) => {
          let node = a;
          let chosen = a;
          for (let i = 0; i < 8 && node; i++) {
            const text = (node.innerText || '').trim();
            const itemLinkCount = node.querySelectorAll('a[href*="/item/"]').length;
            if (itemLinkCount === 1 && text.length >= 5 && text.length <= 900) {
              chosen = node;
            }
            if (itemLinkCount > 1) break;
            node = node.parentElement;
          }
          const img = chosen.querySelector('img');
          return {
            href: a.href,
            text: (chosen.innerText || a.innerText || '').trim(),
            title: (a.getAttribute('aria-label') || a.getAttribute('title') || a.innerText || '').trim(),
            image: img ? (img.currentSrc || img.src || null) : null,
          };
        })
        """
    )
    output: list[Discovery] = []
    for record in records:
        url = canonical_url(str(record.get("href", "")))
        if not url:
            continue
        text = str(record.get("text", ""))
        title = str(record.get("title", "")) or (text.splitlines()[0] if text else "")
        output.append(
            Discovery(
                url=url,
                title=title,
                text=text,
                price=extract_price_from_text(text),
                image=record.get("image"),
                query=query,
                source="dom",
            )
        )
    return output


async def search_query(page: Page, query: str, cfg: dict[str, Any]) -> list[Discovery]:
    settings = cfg["settings"]
    captured: list[Discovery] = []
    response_tasks: list[asyncio.Task[None]] = []

    async def inspect_response(response: Response) -> None:
        try:
            content_type = normalize(response.headers.get("content-type", ""))
            if "json" not in content_type or response.status >= 400:
                return
            if "wallapop" not in normalize(response.url):
                return
            payload = await response.json()
            for obj in walk_json(payload):
                candidate = discovery_from_object(obj, query)
                if candidate:
                    captured.append(candidate)
        except Exception:
            return

    def listener(response: Response) -> None:
        response_tasks.append(asyncio.create_task(inspect_response(response)))

    page.on("response", listener)
    url = settings["search_url_template"].format(query=quote_plus(query))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=int(settings["navigation_timeout_ms"]))
        await accept_cookies(page)
        await page.wait_for_timeout(int(settings["search_page_wait_ms"]))
        # A small scroll triggers lazy-loaded cards without attempting to evade site controls.
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(700)
        body = await page.locator("body").inner_text(timeout=5000)
        title = await page.title()
        if blocked_page(body, title):
            await save_debug(page, f"blocked_search_{query}")
            raise RuntimeError(f"Wallapop blocked the search page for query: {query}")
        captured.extend(await collect_dom_discoveries(page, query))
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
    finally:
        page.remove_listener("response", listener)

    deduped: dict[str, Discovery] = {}
    for item in captured:
        existing = deduped.get(item.url)
        if existing is None or (existing.source == "dom" and item.source == "json"):
            deduped[item.url] = item
        elif existing:
            existing.text = max((existing.text, item.text), key=len)
            existing.title = existing.title or item.title
            existing.price = existing.price if existing.price is not None else item.price
            existing.image = existing.image or item.image
    return list(deduped.values())[: int(settings["max_results_per_query"])]


async def seller_profile_link(page: Page) -> tuple[str | None, str]:
    anchors = page.locator('a[href*="/user/"], a[href*="/profile/"], a[href*="/app/user/"]')
    try:
        count = min(await anchors.count(), 20)
    except Exception:
        return None, "Unknown"
    for index in range(count):
        anchor = anchors.nth(index)
        try:
            href = await anchor.get_attribute("href")
            text = (await anchor.inner_text(timeout=800)).strip()
            if href:
                return urljoin("https://es.wallapop.com", href), text or "Unknown"
        except Exception:
            continue
    return None, "Unknown"


async def verify_listing(page: Page, discovery: Discovery, cfg: dict[str, Any]) -> tuple[Listing | None, str]:
    settings = cfg["settings"]
    try:
        response = await page.goto(discovery.url, wait_until="domcontentloaded", timeout=int(settings["navigation_timeout_ms"]))
        await accept_cookies(page)
        await page.wait_for_timeout(int(settings["listing_page_wait_ms"]))
    except PlaywrightTimeoutError:
        return None, "listing navigation timed out"
    except Exception as exc:
        return None, f"listing navigation failed: {type(exc).__name__}"

    status = response.status if response else 0
    final_url = canonical_url(page.url)
    try:
        body = await page.locator("body").inner_text(timeout=8000)
    except Exception:
        return None, "page body could not be read"
    page_title = await page.title()
    if status in {403, 404, 410, 429} or blocked_page(body, page_title):
        await save_debug(page, f"listing_status_{status}_{item_id_from_url(discovery.url)}")
        return None, f"blocked/unavailable HTTP {status}"
    if not final_url:
        return None, "redirected away from a Wallapop item URL"

    normalized_body = normalize(body)
    inactive_phrases = [
        "este producto ya no esta disponible",
        "este producto ya no está disponible",
        "anuncio no disponible",
        "producto no disponible",
        "el anuncio ha sido eliminado",
        "listing is no longer available",
        "item is no longer available",
        "este producto ya ha sido vendido",
        "producto vendido",
        "este anuncio esta reservado",
        "este anuncio está reservado",
        "producto reservado",
    ]
    if any(normalize(phrase) in normalized_body for phrase in inactive_phrases):
        return None, "listing is sold/reserved/deleted"

    scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
    product = extract_jsonld_product(scripts)
    jsonld_active = availability_is_active(product)
    if jsonld_active is False:
        return None, "structured data marks listing unavailable"

    async def content_attr(selector: str, attr: str = "content") -> str:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                return (await locator.get_attribute(attr) or "").strip()
        except Exception:
            pass
        return ""

    h1_text = ""
    try:
        h1 = page.locator("h1").first
        if await h1.count():
            h1_text = (await h1.inner_text(timeout=1500)).strip()
    except Exception:
        pass

    og_title = await content_attr('meta[property="og:title"]')
    og_description = await content_attr('meta[property="og:description"]')
    meta_description = await content_attr('meta[name="description"]')

    title = (
        str(first(product, "name", default="")).strip()
        or h1_text
        or og_title.replace(" | Wallapop", "").strip()
        or discovery.title.strip()
        or page_title.replace(" | Wallapop", "").strip()
    )
    description = (
        str(first(product, "description", default="")).strip()
        or og_description
        or meta_description
    )
    price = product_price(product) or discovery.price or extract_price_from_text(body)
    if not title:
        return None, "listing title could not be extracted"
    if price is None:
        return None, "listing price could not be extracted"

    main_text = ""
    for selector in ("main", "article", '[role="main"]'):
        try:
            locator = page.locator(selector).first
            if await locator.count():
                candidate = (await locator.inner_text(timeout=2500)).strip()
                if len(candidate) > len(main_text):
                    main_text = candidate
        except Exception:
            continue

    product_block = primary_product_text(main_text or body, title=title)
    # Keep the live direct page as the source of availability/price, but include
    # the matching search-card title/text as relevance evidence. Some Wallapop
    # item pages expose a generic H1 or sparse metadata even when the search card
    # clearly identifies a tracked boot brand/model.
    headline_text = f"{title}\n{discovery.title}"
    if contains_any(headline_text, cfg["excluded_terms"]):
        return None, "explicitly excluded Dr. Martens listing"

    combined = f"{title}\n{description}\n{product_block}\n{discovery.title}\n{discovery.text}"
    if not looks_relevant(combined, cfg):
        return None, "page is not a relevant boot listing"
    size = extract_size(combined, set(settings["sizes"]))
    if size is None:
        return None, "EU size 40-45 could not be verified"
    brand = detect_brand(combined, cfg["brand_aliases"])
    is_exotic = contains_any(combined, cfg["exotic_terms"])
    listing_age_hours, recency_text = extract_listing_recency(product_block or body)

    profile_url, seller_name = await seller_profile_link(page)
    seller_hours, seller_text = extract_seller_activity(product_block or body)
    if profile_url and seller_hours is None:
        profile_page = await page.context.new_page()
        try:
            profile_response = await profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=int(settings["navigation_timeout_ms"]))
            await accept_cookies(profile_page)
            await profile_page.wait_for_timeout(2500)
            profile_body = await profile_page.locator("body").inner_text(timeout=7000)
            if profile_response and profile_response.status < 400 and not blocked_page(profile_body, await profile_page.title()):
                seller_hours, seller_text = extract_seller_activity(profile_body)
                if seller_name == "Unknown":
                    seller_name = (await profile_page.title()).split("|")[0].strip() or "Unknown"
        except Exception:
            pass
        finally:
            await profile_page.close()

    action_terms = ["comprar", "chat", "hacer una oferta", "envio", "envío"]
    purchasable = any(term in normalized_body for term in action_terms)
    active = bool(final_url and jsonld_active is not False)

    return Listing(
        item_id=item_id_from_url(final_url),
        url=final_url,
        title=title.strip(),
        description=description.strip(),
        price=float(price),
        brand=brand,
        size=size,
        condition=extract_condition(product_block or body),
        city=extract_city(product_block or body),
        image=product_image(product) or discovery.image,
        listing_age_hours=listing_age_hours,
        recency_text=recency_text,
        seller_name=seller_name,
        seller_profile_url=profile_url,
        seller_active_hours=seller_hours,
        seller_activity_text=seller_text,
        active=active,
        purchasable=purchasable,
        is_exotic=is_exotic,
        query=discovery.query,
        evidence={"http_status": status, "jsonld_active": jsonld_active, "source": discovery.source},
    ), "verified"


def threshold_for(listing: Listing, cfg: dict[str, Any]) -> float:
    settings = cfg["settings"]
    if listing.is_exotic:
        return float(settings["exotic_max_price"])
    if listing.brand in set(cfg["premium_brands"]):
        return float(settings["premium_max_price"])
    return float(settings["ordinary_max_price"])


def comparable_prices(listing: Listing, state: dict[str, Any]) -> list[float]:
    exact: list[float] = []
    brand_only: list[float] = []
    for sample in state.get("samples", []):
        if sample.get("brand") != listing.brand or listing.brand is None:
            continue
        price = nested_price(sample.get("price"))
        if price is None or price <= 0:
            continue
        brand_only.append(price)
        if sample.get("size") == listing.size:
            exact.append(price)
    return exact if len(exact) >= 5 else brand_only


def market_median(listing: Listing, state: dict[str, Any]) -> float | None:
    prices = comparable_prices(listing, state)
    return float(statistics.median(prices)) if len(prices) >= 5 else None


def qualifies_as_bargain(listing: Listing, state: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, float | None, str]:
    threshold = threshold_for(listing, cfg)
    median = market_median(listing, state)
    if listing.price <= threshold:
        return True, median, f"below the €{threshold:.0f} alert ceiling"
    if median and listing.price <= median * (1 - float(cfg["settings"]["min_discount_pct"]) / 100):
        discount = round((1 - listing.price / median) * 100)
        return True, median, f"approximately {discount}% below the observed median"
    return False, median, "not sufficiently underpriced"


def seller_and_recency_pass(listing: Listing, is_price_drop: bool, cfg: dict[str, Any]) -> tuple[bool, str]:
    settings = cfg["settings"]
    max_age = float(settings["max_listing_age_hours"])
    if not is_price_drop:
        if listing.listing_age_hours is None:
            return False, "listing recency could not be verified"
        if listing.listing_age_hours > max_age:
            return False, f"listing is older than {max_age:.0f} hours"
    if not listing.active or not listing.purchasable:
        return False, "listing is not verifiably active/purchasable"
    seller_hours = listing.seller_active_hours
    if seller_hours is None and bool(settings.get("require_seller_activity", True)):
        return False, "seller activity could not be verified"
    if seller_hours is not None and seller_hours > float(settings["max_seller_inactivity_hours"]):
        # User allowed the exception only for a listing posted/edited within 72 hours.
        if listing.listing_age_hours is None or listing.listing_age_hours > max_age:
            return False, "seller appears inactive for more than 30 days"
    return True, "verified"


def estimate_resale(listing: Listing, median: float | None, cfg: dict[str, Any]) -> tuple[int, int]:
    threshold = threshold_for(listing, cfg)
    if median:
        low = int(max(listing.price, median * 0.82))
        high = int(max(low + 15, median * 1.12))
    else:
        low = int(max(listing.price * 1.2, threshold * 0.72))
        high = int(max(low + 20, threshold * 1.15))
    return low, high


def verdict(listing: Listing, median: float | None, cfg: dict[str, Any]) -> str:
    reference = median or threshold_for(listing, cfg)
    ratio = listing.price / reference if reference else 1
    if ratio <= 0.5:
        return "BUY NOW"
    if ratio <= 0.7:
        return "STRONG BUY"
    return "NEGOTIATE"


def format_hours(hours: float | None) -> str:
    if hours is None:
        return "Unverified"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{round(hours)} h ago"
    return f"{round(hours / 24)} days ago"


def format_alert(listing: Listing, old_price: float | None, median: float | None, reason: str, cfg: dict[str, Any]) -> str:
    resale_low, resale_high = estimate_resale(listing, median, cfg)
    margin_low = max(0, resale_low - round(listing.price))
    margin_high = max(0, resale_high - round(listing.price))
    price_line = f"€{listing.price:.0f}"
    if old_price is not None and listing.price < old_price:
        price_line += f" (down from €{old_price:.0f})"
    brand = listing.brand.title() if listing.brand else "Unbranded / hidden brand"
    size_highlight = " ⭐" if listing.size in set(cfg["settings"]["highlight_sizes"]) else ""
    risk = "Verify inner labels, heel/sole wear, lining, cracks and sole separation"
    if listing.is_exotic:
        risk += "; request proof that the exotic leather is genuine and legally tradeable"
    lines = [
        f"🚨 <b>{html.escape(verdict(listing, median, cfg))}</b>",
        f"<b>{html.escape(listing.title)}</b>",
        f"Brand: {html.escape(brand)}",
        f"Size: EU {listing.size}{size_highlight}",
        f"Price: {html.escape(price_line)}",
        f"Condition: {html.escape(listing.condition)}",
        f"Location: {html.escape(listing.city)}",
        f"Listing recency: {html.escape(listing.recency_text)} ({format_hours(listing.listing_age_hours)})",
        f"Seller activity: {html.escape(listing.seller_activity_text)} ({format_hours(listing.seller_active_hours)})",
        f"Why: {html.escape(reason)}",
        f"Estimated resale: €{resale_low}–€{resale_high}",
        f"Possible gross margin: €{margin_low}–€{margin_high}",
        f"Risk check: {html.escape(risk)}",
        f'🔗 <a href="{html.escape(listing.url, quote=True)}">Open live Wallapop listing</a>',
    ]
    return "\n".join(lines)


def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        endpoint,
        data={
            "chat_id": chat_id,
            "text": message[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def update_sample(state: dict[str, Any], listing: Listing) -> None:
    key = f"{listing.item_id}:{listing.price:.2f}"
    samples = state.setdefault("samples", [])
    if any(sample.get("key") == key for sample in samples):
        return
    samples.append(
        {
            "key": key,
            "item_id": listing.item_id,
            "brand": listing.brand,
            "size": listing.size,
            "price": listing.price,
            "seen_at": utc_now().isoformat(),
        }
    )
    # Keep state reasonably small in the Git repository.
    state["samples"] = samples[-2500:]


def update_seen(state: dict[str, Any], listing: Listing) -> None:
    now_iso = utc_now().isoformat()
    previous = state.setdefault("seen", {}).get(listing.item_id, {})
    state["seen"][listing.item_id] = {
        "url": listing.url,
        "title": listing.title,
        "brand": listing.brand,
        "size": listing.size,
        "price": listing.price,
        "first_seen": previous.get("first_seen", now_iso),
        "last_seen": now_iso,
        "last_listing_age_hours": listing.listing_age_hours,
        "seller_active_hours": listing.seller_active_hours,
    }


def prune_state(state: dict[str, Any]) -> None:
    cutoff_seconds = 180 * 24 * 3600
    now = utc_now()
    kept: dict[str, Any] = {}
    for item_id, record in state.get("seen", {}).items():
        try:
            dt = datetime.fromisoformat(record.get("last_seen", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt).total_seconds() <= cutoff_seconds:
                kept[item_id] = record
        except (ValueError, TypeError):
            kept[item_id] = record
    state["seen"] = kept


def all_queries(cfg: dict[str, Any], state: dict[str, Any]) -> list[str]:
    base = list(cfg["hourly_queries"])
    batches = list(cfg["rotating_query_batches"])
    if cfg["settings"].get("run_all_queries_every_hour", False):
        extras = [query for batch in batches for query in batch]
    else:
        run_count = int(state.get("run_count", 0))
        extras = list(batches[run_count % len(batches)]) if batches else []
    return list(dict.fromkeys(base + extras))


def discovery_priority(item: Discovery, state: dict[str, Any], cfg: dict[str, Any]) -> tuple[int, int, int, int, int, int, float]:
    item_id = item_id_from_url(item.url)
    seen = state.get("seen", {}).get(item_id)
    old_price = nested_price(seen.get("price")) if seen else None
    price_drop = old_price is not None and item.price is not None and item.price < old_price
    title = item.title or ""
    text = f"{title} {item.text}"
    title_brand = detect_brand(title, cfg["brand_aliases"])
    card_brand = detect_brand(text, cfg["brand_aliases"])
    query_brand = detect_brand(item.query, cfg["brand_aliases"])
    title_has_boot = contains_any(title, cfg["boot_terms"])
    card_has_boot = contains_any(text, cfg["boot_terms"])
    size = extract_size(text, set(cfg["settings"]["sizes"]))
    query_match = query_brand is not None and card_brand == query_brand
    return (
        0 if price_drop else 1,
        0 if not seen else 1,
        0 if title_has_boot else 1,
        0 if title_brand else 1,
        0 if query_match else 1,
        0 if size else 1,
        item.price if item.price is not None else 99999,
    )


def price_recheck_discoveries(state: dict[str, Any], cfg: dict[str, Any]) -> list[Discovery]:
    records = list(state.get("seen", {}).values())
    if not records:
        return []
    records.sort(key=lambda record: record.get("last_seen", ""))
    count = min(int(cfg["settings"]["price_rechecks_per_run"]), len(records))
    cursor = int(state.get("price_recheck_cursor", 0)) % len(records)
    selected = [records[(cursor + offset) % len(records)] for offset in range(count)]
    state["price_recheck_cursor"] = (cursor + count) % len(records)
    output: list[Discovery] = []
    for record in selected:
        url = canonical_url(str(record.get("url", "")))
        if url:
            output.append(
                Discovery(
                    url=url,
                    title=str(record.get("title", "")),
                    price=nested_price(record.get("price")),
                    query="price recheck",
                    source="state",
                )
            )
    return output


async def create_context(browser: Browser, cfg: dict[str, Any]) -> BrowserContext:
    return await browser.new_context(
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": 1440, "height": 1100},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.7"},
    )


async def run() -> int:
    cfg = load_config()
    state = load_state()
    settings = cfg["settings"]
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    send_test = os.getenv("SEND_TEST_ALERT", "0") == "1"
    initialized = bool(state.get("initialized", False))
    silent_bootstrap = bool(settings.get("bootstrap_silent", True)) and not initialized and not dry_run

    if send_test:
        telegram_send("✅ <b>Wallapop Boot Watch test successful</b>\nTelegram alerts are connected.")
        LOG.info("Test Telegram alert sent.")
        return 0

    queries = all_queries(cfg, state)
    LOG.info("Starting %d searches. Full hourly mode: %s", len(queries), settings.get("run_all_queries_every_hour"))
    discoveries: dict[str, Discovery] = {}
    search_failures = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=bool(settings.get("headless", True)))
        context = await create_context(browser, cfg)
        search_page = await context.new_page()

        for index, query in enumerate(queries, start=1):
            try:
                results = await search_query(search_page, query, cfg)
                LOG.info("[%d/%d] %s: %d candidate links", index, len(queries), query, len(results))
                for item in results:
                    if not discovery_maybe_relevant(item, cfg):
                        continue
                    existing = discoveries.get(item.url)
                    if existing is None or (existing.source == "dom" and item.source == "json"):
                        discoveries[item.url] = item
            except RuntimeError as exc:
                search_failures += 1
                LOG.error("%s", exc)
                # A challenge/403 is unlikely to improve by hammering more searches.
                if search_failures >= 2:
                    LOG.error("Stopping this run after repeated blocking. No protection bypass is attempted.")
                    break
            except Exception as exc:
                search_failures += 1
                LOG.warning("Search failed for %s: %s", query, exc)
            await search_page.wait_for_timeout(int(settings["polite_delay_ms"]) + random.randint(0, 350))

        await search_page.close()

        # Add a rotating set of already-seen listings so real price changes are detected.
        for item in price_recheck_discoveries(state, cfg):
            discoveries.setdefault(item.url, item)

        ordered = sorted(discoveries.values(), key=lambda item: discovery_priority(item, state, cfg))
        ordered = ordered[: int(settings["max_listing_verifications_per_run"])]
        LOG.info("Verifying %d direct listing pages", len(ordered))

        listing_page = await context.new_page()
        alerts_sent = 0
        verified_count = 0
        rejection_counts: dict[str, int] = {}
        for index, discovery in enumerate(ordered, start=1):
            listing, rejection_reason = await verify_listing(listing_page, discovery, cfg)
            if listing is None:
                rejection_counts[rejection_reason] = rejection_counts.get(rejection_reason, 0) + 1
                LOG.info(
                    "[%d/%d] REJECT %s | query=%s | reason=%s",
                    index,
                    len(ordered),
                    (discovery.title or discovery.url.rsplit("/", 1)[-1])[:60],
                    discovery.query,
                    rejection_reason,
                )
                continue
            verified_count += 1
            previous = state.get("seen", {}).get(listing.item_id)
            old_price = nested_price(previous.get("price")) if previous else None
            is_price_drop = old_price is not None and listing.price < old_price
            is_new = previous is None

            update_sample(state, listing)
            update_seen(state, listing)

            qualifies, median, reason = qualifies_as_bargain(listing, state, cfg)
            rules_pass, rule_reason = seller_and_recency_pass(listing, is_price_drop=is_price_drop, cfg=cfg)
            should_alert = qualifies and rules_pass and (is_new or is_price_drop)

            LOG.info(
                "[%d/%d] %s | €%.0f | size %s | new=%s drop=%s bargain=%s rules=%s reason=%s",
                index,
                len(ordered),
                listing.title[:55],
                listing.price,
                listing.size,
                is_new,
                is_price_drop,
                qualifies,
                rules_pass,
                "verified" if rules_pass else rule_reason,
            )

            if should_alert and not silent_bootstrap:
                message = format_alert(listing, old_price, median, reason, cfg)
                if dry_run:
                    print("\n--- DRY RUN ALERT ---\n" + re.sub(r"<[^>]+>", "", message))
                else:
                    telegram_send(message)
                alerts_sent += 1

            await listing_page.wait_for_timeout(int(settings["polite_delay_ms"]) + random.randint(0, 350))

        await listing_page.close()
        await context.close()
        await browser.close()

    state["initialized"] = True
    state["last_run"] = utc_now().isoformat()
    state["run_count"] = int(state.get("run_count", 0)) + 1
    prune_state(state)
    if dry_run:
        LOG.info("Dry run completed: data/state.json was not modified.")
    else:
        save_state(state)

    if 'rejection_counts' in locals() and rejection_counts:
        summary = "; ".join(f"{reason}={count}" for reason, count in sorted(rejection_counts.items(), key=lambda pair: pair[1], reverse=True))
        LOG.info("Verification rejection summary: %s", summary)

    LOG.info(
        "Finished. Searches=%d failures=%d discovered=%d verified=%d alerts=%d bootstrap_silent=%s",
        len(queries),
        search_failures,
        len(discoveries),
        verified_count,
        alerts_sent,
        silent_bootstrap,
    )
    if search_failures >= 2 and verified_count == 0:
        # Fail the workflow so the debug screenshot/HTML is uploaded.
        return 2
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception:
        LOG.exception("Fatal watcher error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
