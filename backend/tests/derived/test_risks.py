from decimal import Decimal

from app.stocks.derived.risks import RiskItem, score_risks


def _risk(status: str) -> RiskItem:
    return RiskItem(status, status, "test", {}, (), status)


def test_unknown_does_not_count_as_normal() -> None:
    summary = score_risks([_risk("risk"), _risk("unknown"), _risk("normal")])
    assert summary.score == Decimal("50.00")
    assert summary.coverage == Decimal("0.6667")


def test_score_is_null_below_sixty_percent_coverage() -> None:
    assert score_risks([_risk("risk"), _risk("unknown"), _risk("unknown")]).score is None
