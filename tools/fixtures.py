"""Explicitly fictional monitoring and integration fixtures."""
import copy
from src.publication import content_hash_for_approval


PROPERTY_ID = "c4b69f48-2ab2-43ed-9d90-976ba62bf001"


def sample_property():
    def fact(value):
        return {"value": value, "verification": "confirmed", "conflict": False, "source": "educational-fixture"}
    p = {"id": PROPERTY_ID, "version": 7, "educational": True,
         "facts": {"address": fact("Учебный город, Тестовая улица, 12, помещение 2"), "area_m2": fact(118), "price_rub": fact(24000000), "category": fact("commercial"), "deal_type": fact("sale")},
         "description": "Учебное торговое помещение. Все сведения и стоимость вымышлены. Не публиковать.",
         "images": [{"asset_id": "demo-front-01", "rights_confirmed": True, "mime": "image/jpeg", "width": 1280, "height": 720}],
         "advertising": {}}
    p["approval"] = {"approved": True, "by": "demo-reviewer", "for_content_hash": content_hash_for_approval(p)}
    return p


def monitoring_batches():
    def row(src, lid, address, area, price, desc, cad=None):
        return {"source": src, "listing_id": lid, "url": f"https://example.invalid/{src}/{lid}", "address": address,
                "area_m2": area, "price_rub": price, "description": desc, "unit_cadastral_id": cad,
                "status": "listed", "educational": True}
    first = [
        row("partner-a", "A101", "Учебный город, Лесная 10, пом. 1", 110, 22000000, "ГАБ с арендатором — продукты", "DEMO-UNIT-01"),
        row("partner-b", "B901", "Учебный город, Лесная 10, пом. 1", 110, 22500000, "Действующий арендатор", "DEMO-UNIT-01"),
        row("partner-a", "A102", "Учебный город, Полевая 8, пом. 2", 82, 12000000, "Без арендатора. Свободное помещение", "DEMO-UNIT-02"),
        row("partner-b", "B902", "Учебный город, Парковая 4", 200, None, "Торговое помещение"),
        row("partner-b", "B903", "Учебный город, Парковая 4", 201, 30000000, "ГАБ. Помещение свободно"),
        row("partner-a", "A103", "Учебный город, Южная 3", 66, 9500000, "ГАБ с арендатором")]
    ok = {"partner-a": {"status": "ok", "complete": True, "expected_min": 3}, "partner-b": {"status": "ok", "complete": True, "expected_min": 3}}
    second = copy.deepcopy(first)
    second[0]["price_rub"] = 21000000
    second = [r for r in second if r["listing_id"] != "A102"]
    second[1]["status"] = "withdrawn_by_source"
    second.append(row("partner-a", "A104", "Учебный город, Садовая 16", 140, 19500000, "Помещение свободно"))
    third = [copy.deepcopy(r) for r in second if r["source"] == "partner-a"]
    return [
        {"run_id": "2026-11-01", "captured_at": "2026-11-01T08:00:00+03:00", "sources": ok, "listings": first},
        {"run_id": "2026-11-02", "captured_at": "2026-11-02T08:00:00+03:00", "sources": ok, "listings": second},
        {"run_id": "2026-11-03", "captured_at": "2026-11-03T08:00:00+03:00", "sources": {"partner-a": {"status": "ok", "complete": True, "expected_min": 3}, "partner-b": {"status": "failed", "complete": False, "reason": "HTTP 503 — simulated"}}, "listings": third}]
