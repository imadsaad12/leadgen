from pathlib import Path

import pytest

from leadgen.config import DATA_DIR
from leadgen.pipeline.geo import country_names, expand_location, hq_matches

needs_gazetteer = pytest.mark.skipif(not (Path(DATA_DIR) / "locations.yaml").exists(), reason="locations.yaml missing")


def test_literal_match_without_gazetteer():
    assert hq_matches("Berlin, Berlin", ["Berlin"]) is True
    assert hq_matches("Beirut, Lebanon", ["Germany"]) is False
    assert hq_matches(None, ["Germany"]) is None
    assert hq_matches("Paris, Ile-de-France", ["Germany"]) is False  # "de" must not match
    assert hq_matches("San Francisco, California", ["CA"]) is False  # 2-letter codes never match free text


@needs_gazetteer
def test_country_expands_to_cities_and_regions():
    terms = expand_location("Germany")
    assert "berlin" in terms and "munich" in terms and "bavaria" in terms
    assert hq_matches("Berlin, Berlin", ["Germany"]) is True
    assert hq_matches("München, Bayern", ["Germany"]) is True
    assert hq_matches("Vienna, Vienna", ["Germany"]) is False
    assert hq_matches("Vienna, Vienna", ["Austria"]) is True
    assert hq_matches("Dubai, United Arab Emirates", ["UAE"]) is True
    assert hq_matches("London, England", ["United Kingdom"]) is True
    assert hq_matches("Beirut, Lebanon", ["Lebanon"]) is True
    assert "Germany" in country_names() and len(country_names()) > 100
