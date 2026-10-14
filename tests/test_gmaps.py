import json

from leadgen.sources.gmaps import GmapsClient, build_queries, clean_emails, parse_results
from leadgen.sources.osm import Business


def test_build_queries():
    q = build_queries("Lebanon", "Beirut", ["healthcare", "food"])
    assert "pharmacies in Beirut, Lebanon" in q and "restaurants in Beirut, Lebanon" in q
    assert all("Beirut, Lebanon" in x for x in q)


def test_clean_emails_drops_placeholders():
    got = clean_emails(["Info@Clinic.lb", "john@doe.com", "hello@example.com", "noreply@x.com", "real@biz.co", "name@domain"])
    assert got == ["info@clinic.lb", "real@biz.co"]


def test_parse_results_jsonl(tmp_path):
    rows = [
        {"title": "Beirut Pharmacy", "category": "Pharmacy", "phone": "+9611", "web_site": "https://rx.lb",
         "emails": ["info@rx.lb", "john@doe.com"], "latitude": 33.9, "longitude": 35.5, "place_id": "p1"},
        {"title": "Cedar Clinic", "categories": ["Medical clinic"], "phone": "+9612", "emails": [], "place_id": "p2"},
        {"title": "Beirut Pharmacy", "phone": "+9611", "place_id": "p1"},  # duplicate
        {"phone": "+9613"},  # no title -> skipped
    ]
    f = tmp_path / "results.json"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    biz = parse_results(f)
    assert [b.name for b in biz] == ["Beirut Pharmacy", "Cedar Clinic"]
    assert biz[0].website == "https://rx.lb" and biz[0].email == "info@rx.lb"  # placeholder dropped
    assert biz[1].category == "Medical clinic" and biz[1].email is None


def test_parse_results_json_array(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps([{"title": "X", "phone": "+1", "emails": ["a@b.co"]}]))
    biz = parse_results(f)
    assert len(biz) == 1 and biz[0].email == "a@b.co"
