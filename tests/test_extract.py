from leadgen.extract.companies import parse_company_about, parse_company_search
from leadgen.extract.people import parse_people_page
from leadgen.models import Company
from leadgen.sources.fake import COMPANIES, _people_for, about_payload, company_search_payload, people_page_payload


def test_people_search_page_parses_cards_and_drops_anonymous():
    data = COMPANIES[0]
    people = _people_for(data)[:10]
    payload = people_page_payload(data, people, 1, 140)
    parsed = parse_people_page(payload, company_key=data["slug"], company_name=data["name"], found_via="t")
    assert parsed.status == "ok", parsed.notes
    assert parsed.references == 11  # 10 + anonymized member
    real = [p for p in parsed.people if not p.anonymized]
    assert len(real) == 10
    assert all(p.raw_title for p in real)
    assert all(p.location for p in real)
    assert all(p.degree in ("1st", "2nd", "3rd+") for p in real)
    first = real[0]
    assert first.username == people[0]["username"]
    assert first.full_name == people[0]["name"]
    assert first.raw_title == people[0]["title"]
    anon = [p for p in parsed.people if p.anonymized]
    assert len(anon) == 1 and anon[0].username.startswith("ACoAA")


def test_people_page_without_text_is_reference_only():
    payload = {"url": "x", "sections": {}, "references": {"search_results": [{"kind": "person", "url": "/in/jane-doe-1a2b/"}]}}
    parsed = parse_people_page(payload, company_key="c")
    assert parsed.status == "degraded"
    assert parsed.people[0].full_name == "Jane Doe"
    assert parsed.people[0].parse_method == "reference_only" and parsed.people[0].needs_review


def test_employees_tab_function_breakdown_and_total():
    data = COMPANIES[0]
    payload = people_page_payload(data, _people_for(data)[:5], 1, 140, kind="employees")
    parsed = parse_people_page(payload, company_key=data["slug"], found_via="employees")
    assert parsed.total_hint == 140
    assert parsed.function_breakdown["Engineering"] == 41
    assert parsed.function_breakdown["Sales"] == 25
    assert len(parsed.people) == 5


def test_headline_variants():
    data = COMPANIES[0]
    people = [
        {"username": "a-b", "name": "A B", "degree": "2nd", "headline": "CTO | Co-founder", "location": "Berlin, Germany"},
        {"username": "c-d", "name": "C D", "degree": "1st", "headline": "Head of Sales at Acme GmbH", "location": "Munich, Bavaria, Germany"},
        {"username": "e-f", "name": "E F", "degree": "3rd+", "headline": "Berlin, Germany", "location": "Germany"},
    ]
    payload = people_page_payload(data, people, 1, 3)
    parsed = parse_people_page(payload, company_key="k", company_name=data["name"])
    by = {p.username: p for p in parsed.people}
    assert by["a-b"].raw_title == "CTO | Co-founder" and by["a-b"].company_name == data["name"]
    assert by["c-d"].raw_title == "Head of Sales" and by["c-d"].company_name == "Acme GmbH"
    assert by["e-f"].raw_title is None and by["e-f"].needs_review


def test_company_search_and_about():
    parsed = parse_company_search(company_search_payload(COMPANIES[:3]))
    assert parsed.status == "ok"
    assert [c.key for c in parsed.companies] == [c["slug"] for c in COMPANIES[:3]]
    assert parsed.companies[0].industry == "Financial Services"
    assert parsed.companies[0].hq == "Berlin, Berlin"
    about = parse_company_about(about_payload(COMPANIES[0]), parsed.companies[0])
    assert about.size_bucket == "51-200"
    assert about.founded == 2016 and about.company_type == "Privately Held"
    assert about.website.startswith("https://") and about.urn == "71001"
    assert about.employee_count_hint == 140
    assert "payments" in about.specialties and about.status == "profiled"
    assert about.unknown_fields == []
    noname = parse_company_about({"sections": {"about": "Something\nIndustry\nRetail"}, "references": {}},
                                 Company(key="x", name="X"))
    assert noname.industry == "Retail" and "urn" in noname.unknown_fields
