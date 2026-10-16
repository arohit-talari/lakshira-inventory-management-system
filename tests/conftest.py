import pytest

from tests import fixtures_data as fd


@pytest.fixture(autouse=True)
def _cleanup_fixture_rows():
    """Delete every row create_sold_unit()/create_sold_unit_blank_total_cost()/
    create_partial_payment_unit()/create_expense_row()/
    create_recurring_registry_row() wrote during this test, pass or fail --
    without this, Tier 2 runs leave permanent rows behind on every run (see
    fixtures_data._created_skus/_created_expense_ids/_created_registry_ids)."""
    yield
    fd.cleanup_created_units()
    fd.cleanup_created_expenses()
    fd.cleanup_created_registries()
