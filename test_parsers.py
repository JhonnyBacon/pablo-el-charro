from watcher import (
    canonical_url,
    extract_listing_recency,
    extract_price_from_text,
    extract_seller_activity,
    extract_size,
    parse_relative_hours,
)


def test_relative_hours():
    assert parse_relative_hours("hace 30 minutos") == 0.5
    assert parse_relative_hours("activo hace 2 horas") == 2
    assert parse_relative_hours("editado hace 3 días") == 72
    assert parse_relative_hours("hace menos de una hora") == 0.5


def test_recency():
    hours, phrase = extract_listing_recency("Editado hace 2 horas\nMadrid")
    assert hours == 2
    assert "editado" in phrase


def test_seller_activity():
    hours, phrase = extract_seller_activity("Marcos\nActivo hace 5 minutos")
    assert round(hours, 4) == round(5 / 60, 4)
    assert "activo" in phrase


def test_size_and_price():
    assert extract_size("Botas Sendra talla 43", {40, 41, 42, 43, 44, 45}) == 43
    assert extract_price_from_text("Como nuevas\n75 €\nBarcelona") == 75


def test_canonical_url():
    assert canonical_url("/item/botas-sendra-123456") == "https://es.wallapop.com/item/botas-sendra-123456"

from watcher import load_config, looks_relevant, primary_product_text


def test_primary_product_text_stops_before_recommendations():
    body = "Botas Sendra talla 43\n90 €\nBuen estado\nProductos similares\nLibro talla 44\n"
    assert "Libro" not in primary_product_text(body)


def test_relevance_rejects_book_collision():
    cfg = load_config()
    assert not looks_relevant("Libro: Viaje a la Alcarria, Las botas de siete leguas, talla 45", cfg)
    assert looks_relevant("Botas Tony Mora de piel talla 43", cfg)
