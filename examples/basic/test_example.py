from collections.abc import AsyncIterator

from testenix import case, cases, fixture, skip, test, xfail


@fixture(scope="module")
async def multiplier() -> AsyncIterator[int]:
    yield 2


@test("multiplication uses a typed async fixture", tags={"unit"})
@cases(
    case(id="positive", value=3, expected=6),
    case(id="zero", value=0, expected=0),
)
async def multiplication(multiplier: int, value: int, expected: int) -> None:
    assert multiplier * value == expected


@skip("demonstrates an explicit skip")
def test_skipped_example() -> None:
    raise AssertionError("a skipped test must not execute")


@xfail("known example defect")
def test_expected_failure() -> None:
    assert 1 == 2
