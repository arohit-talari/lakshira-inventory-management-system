import pytest

from tests import fixtures_data as fd


@pytest.fixture(autouse=True)
def _cleanup_fixture_rows():
    """Delete every row create_sold_unit()/create_sold_unit_blank_total_cost()/
    create_partial_payment_unit() wrote during this test, pass or fail --
    without this, Tier 2 runs leave permanent customer rows behind on every
    run (see fixtures_data._created_skus)."""
    yield
    fd.cleanup_created_units()
