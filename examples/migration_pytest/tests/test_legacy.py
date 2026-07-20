import pytest


@pytest.fixture
def factor():
    return 3


@pytest.mark.parametrize(
    "value, expected",
    [(2, 6), pytest.param(4, 12, id="larger")],
)
def test_multiplies(factor, value, expected):
    assert factor * value == expected


@pytest.mark.skip(reason="documented legacy skip")
def test_future_behavior():
    raise AssertionError("skipped")
