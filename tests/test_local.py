import pytest

from leadgen.sources.local import LocalSource, local_spec
from leadgen.sources.osm import Business, OSMClient


def test_build_query_and_parse():
    bbox = (33.8, 35.4, 33.95, 35.6)
    q = OSMClient.build_query(bbox, ["healthcare", "food"], limit=50)
    assert '["amenity"~"^(' in q and '"phone"' in q and "out center 50" in q and "33.8,35.4,33.95,35.6" in q
    els = [
        {"type": "node", "id": 1, "lat": 33.9, "lon": 35.5,
         "tags": {"name": "Beirut Clinic", "healthcare": "clinic", "phone": "+9611111111", "website": "https://bc.lb"}},
        {"type": "node", "id": 2, "lat": 33.9, "lon": 35.5,
         "tags": {"name": "Cafe X", "amenity": "cafe", "contact:phone": "+9612222222"}},
        {"type": "node", "id": 3, "tags": {"amenity": "cafe", "phone": "+9613"}},  # no name -> dropped
        {"type": "node", "id": 4, "tags": {"name": "Beirut Clinic", "healthcare": "clinic", "phone": "+9611111111"}},  # dup
    ]
    biz = OSMClient.parse(els)
    assert [b.name for b in biz] == ["Beirut Clinic", "Cafe X"]
    assert biz[0].category == "clinic" and biz[0].phone == "+9611111111" and biz[0].website == "https://bc.lb"
    assert biz[1].phone == "+9612222222"


def test_local_spec():
    s = local_spec("Lebanon", "Beirut", ["healthcare"], max_results=30, offer="CPaaS")
    assert s.run.source == "local" and s.run.strategy == "people_first"
    assert s.company.locations == ["Lebanon"] and s.company.city == "Beirut"
    assert s.company.categories == ["healthcare"] and s.company.max_companies == 30 and s.offer == "CPaaS"


class FakeOSM(OSMClient):
    def businesses(self, country, city, categories, limit=200):
        return [
            Business(osm_id="node/1", name="Beirut Pharmacy", category="pharmacy", phone="+9611234567",
                     website="https://rx.lb", city="Beirut", country=country, lat=33.9, lon=35.5),
            Business(osm_id="node/2", name="Cedar Clinic", category="clinic", phone="+9617654321",
                     whatsapp="+9617654321", city="Beirut", country=country, lat=33.9, lon=35.5),
        ]


@pytest.mark.asyncio
async def test_local_source_search_leads_through_pipeline(store, pipeline):
    import asyncio
    from contextlib import asynccontextmanager

    spec = local_spec("Lebanon", "Beirut", ["healthcare"], max_results=50)
    run = store.create_run(spec)

    @asynccontextmanager
    async def factory(ctx):
        yield LocalSource(client=FakeOSM())

    result = await pipeline.execute(run, factory, pause=asyncio.Event(), stop=asyncio.Event())
    assert result.status == "done", result.message
    rows = store.list_people(run.id, limit=100)
    assert {p.full_name for p, _ in rows} == {"Beirut Pharmacy", "Cedar Clinic"}
    by = {p.full_name: p for p, _ in rows}
    assert by["Beirut Pharmacy"].phone == "+9611234567" and by["Beirut Pharmacy"].website == "https://rx.lb"
    assert by["Cedar Clinic"].phone == "+9617654321"  # whatsapp preferred
    assert all(p.location == "Beirut, Lebanon" for p, _ in rows)
    companies = store.list_companies(run.id)
    assert len(companies) == 2
    # export carries the phone column
    from leadgen.export.report import lead_rows, LEAD_COLUMNS
    assert "phone" in LEAD_COLUMNS and "website" in LEAD_COLUMNS
    r = {row["full_name"]: row for row in lead_rows(run.id, store)}
    assert r["Beirut Pharmacy"]["phone"] == "+9611234567"
