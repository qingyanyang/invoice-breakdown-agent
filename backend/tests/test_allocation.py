from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

import pytest

from app.features.allocation.calculate import AllocationPolicy, Scale, allocate
from app.features.classification.calculate import Trade

TARGET = Decimal("282.54")


def line(number, amount, trade="Other", area="Additional Items", *, addition=False):
    return {
        "row": f"Trade Summary!D{number}", "area": area, "trade": trade, "amount": amount,
        "scope_origin": "authorized_addition" if addition else "in_source",
        "protected_by_default": trade in {Trade.MAKE_GOOD, Trade.ACCESS_PANEL},
    }


def reports(lines, *, source_basis="excluding_gst"):
    source = []
    added = []
    for entry in lines:
        reference = {"sheet": "Trade Summary", "cell": entry["row"].split("!")[1]}
        if entry["scope_origin"] == "authorized_addition":
            added.append({"reference": reference, "effect": entry["amount"]})
        else:
            source.append({"reference": reference, "contribution": entry["amount"]})
    total = sum(Decimal(item["amount"]) for item in lines)
    cents = str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return (
        {
            "selected_rows": source, "scope_additions": added, "scope_exclusions": [],
            "gst_basis": source_basis, "working_scope_total": cents,
        },
        {
            "assignments": lines, "classified_total_exact": str(total),
            "gst_basis": source_basis, "working_scope_total": cents,
        },
    )


def decision(lines, target, *, target_basis="excluding_gst", source_basis="excluding_gst", **policy):
    return allocate(*reports(lines, source_basis=source_basis), target, target_basis, AllocationPolicy(**policy))


def independently_rounded(amount: Fraction):
    signed_cents = amount * 100
    absolute = abs(signed_cents)
    rounded = (absolute + Fraction(1, 2)) // 1
    return Decimal(rounded if signed_cents >= 0 else -rounded) / 100


def test_protected_addition_is_counted_once_and_fixture_residual_is_derived():
    lines = [
        line(4, "110", "Partition"), line(5, "55", "Glazing"),
        line(7, "60", "Doors"), line(8, "-10", "Doors"),
        line(10, "25", "Make Good"), line(11, "20", "Access Panel"),
        line(13, "1.01", "Access Panel", addition=True),
    ]
    result = decision(lines, TARGET, requested_direction="decrease")
    assert result["status"] == "ready_for_workbook", result
    assert result["warnings"] == ["direction_mismatch"]

    working = sum(Fraction(item["amount"]) for item in lines)
    protected = sum(Fraction(item["amount"]) for item in lines if item["protected_by_default"])
    factor = (Fraction(TARGET) - protected) / (working - protected)
    assert (Fraction(result["working_total"]), Fraction(result["protected_total"])) == (working, protected)
    assert result["protected_in_source"] == "45"
    assert result["protected_authorized_additions"] == "1.01"
    assert Fraction(result["factor"]["numerator"]) / Fraction(result["factor"]["denominator"]) == factor

    eligible_trades = ("Doors", "Glazing", "Partition")
    amounts = {trade: sum(Fraction(item["amount"]) for item in lines if item["trade"] == trade)
               for trade in eligible_trades}
    displayed = {trade: independently_rounded(amount * factor) for trade, amount in amounts.items()}
    gap = TARGET - independently_rounded(protected) - sum(displayed.values())
    recipient = max(amounts, key=amounts.get)
    displayed[recipient] += gap
    assert result["display"]["rounding"]["recipient"]["trade"] == recipient == "Partition"
    assert Decimal(result["display"]["rounding"]["residual_applied"]) == gap == Decimal("-0.01")
    assert {key: Decimal(result["display"]["trade_totals"][key]) for key in eligible_trades} == displayed
    assert result["display"]["rounding"]["difference"] == "0.00"

    credit = next(item for item in result["assignments"] if item["row"] == "Trade Summary!D8")
    assert Decimal(credit["unrounded_amount"]) < 0
    assert decision(list(reversed(lines)), TARGET, requested_direction="decrease") == result


def test_mixed_cell_preserves_protected_component_through_rounding():
    lines = [
        line(4, "1.005", "Partition", "Area A"),
        line(5, "1.00", "Partition", "Area A"),
        line(6, "1.00", "Partition", "Area B"),
    ]
    target = Decimal("3.02")
    policy = {"protected_rows": ["Trade Summary!D4"]}
    result = decision(lines, target, **policy)
    first, second = result["display"]["cells"]
    assert result["status"] == "ready_for_workbook"
    assert result["display"]["rounding"]["rounded_component_count"] == 3
    assert result["display"]["rounding"]["maximum_difference"] == "0.015"
    assert result["display"]["rounding"]["recipient"] == {"area": "Area A", "trade": "Partition"}
    assert result["display"]["trade_totals"] == {"Partition": "3.02"}
    assert result["display"]["area_totals"] == {"Area A": "2.01", "Area B": "1.01"}

    assert first["protected_exact"] == "1.005"
    assert first["protected_display"] == "1.01"
    assert first["adjustable_display_before_residual"] == "1.01"
    assert first["adjustable_display"] == "1.00"
    assert second["adjustable_display"] == "1.01"
    assert result["assignments"][0]["unrounded_amount"] == "1.005"

    preferred = decision(lines, target, **policy, rounding_preference={"area": "Area B", "trade": "Partition"})
    assert preferred["display"]["cells"][0]["protected_display"] == "1.01"
    assert preferred["display"]["rounding"]["recipient"]["area"] == "Area B"


def test_residual_does_not_turn_a_negative_component_positive():
    lines = [line(index, "0.01", area=f"Area {index}") for index in range(2, 6)]
    lines.append(line(6, "-0.01", area="Area 1"))
    target = Decimal("0.01")
    automatic = decision(lines, target)
    assert automatic["status"] == "ready_for_workbook"
    assert automatic["display"]["rounding"]["recipient"] == {"area": "Area 2", "trade": "Other"}
    assert automatic["display"]["cells"][0]["display_total"] == "0.00"
    preferred = decision(lines, target, rounding_preference={"area": "Area 1", "trade": "Other"})
    assert preferred["issue"]["code"] == "unsafe_rounding_residual"


def test_no_target_preserves_source_and_separately_reports_display_rounding():
    lines = [line(4, "1.005", "Doors"), line(5, "1.005", "Access Panel")]
    result = decision(lines, None, target_basis=None, source_basis="including_gst")
    assert result["status"] == "ready_for_workbook"
    assert result["mode"] == "no_contract_price_adjustment"
    assert result["factor"] is None and result["target"] is None
    assert all(entry["source_amount"] == entry["unrounded_amount"] for entry in result["assignments"])
    rounding = result["display"]["rounding"]
    assert rounding["purpose"] == "source_presentation"
    assert rounding["comparison_total"] == "2.01"
    assert rounding["residual_applied"] == "0.00"
    assert rounding["difference"] == "0.01"


def test_protected_only_can_finish_when_target_equals_protected_total():
    result = decision([line(4, "10", "Make Good")], Decimal("10.00"))
    assert result["status"] == "ready_for_workbook"
    assert result["mode"] == "target_allocation" and result["factor"] is None
    assert result["display"]["rounding"]["difference"] == "0.00"


@pytest.mark.parametrize("lines,target,target_basis,source_basis,code", [
    ([line(4, "10", "Make Good")], "10.01", "excluding_gst", "excluding_gst", "unattainable_target"),
    ([line(4, "10", "Make Good")], "9.99", "excluding_gst", "excluding_gst", "target_below_protected"),
    ([line(4, "-1")], "1.00", "excluding_gst", "excluding_gst", "negative_adjustable_base"),
    ([line(4, "3"), line(5, "-1", "Doors")], "2.00", "excluding_gst", "excluding_gst", "negative_trade_allocation"),
    ([line(4, "1")], "1.00", "unknown", "excluding_gst", "target_gst_unconfirmed"),
    ([line(4, "1")], "1.00", "including_gst", "excluding_gst", "gst_conversion_required"),
    ([line(4, "1")], "1.00", "excluding_gst", "including_gst", "gst_conversion_required"),
])
def test_infeasible_targets_and_unconfirmed_basis_do_not_publish(lines, target, target_basis, source_basis, code):
    result = decision(lines, Decimal(target), target_basis=target_basis, source_basis=source_basis)
    assert result["status"] == "blocked" and result["issue"]["code"] == code
    assert "display" not in result and "assignments" not in result


def test_scope_verification_rejects_offsetting_assignment_errors():
    reconciliation, classification = reports([line(4, "10"), line(5, "-2")])
    classification["assignments"][0]["amount"] = "11"
    classification["assignments"][1]["amount"] = "-3"
    result = allocate(reconciliation, classification, Decimal("8"), "excluding_gst", AllocationPolicy())
    assert result["issue"]["code"] == "working_scope_mismatch"


def test_excessive_difference_blocks_publication(monkeypatch):
    original = Scale.rounded

    def invalid_round(self, source):
        return original(self, source) + Decimal("0.02")

    monkeypatch.setattr(Scale, "rounded", invalid_round)
    result = decision([line(4, "10")], Decimal("12.00"))
    assert result["issue"]["code"] == "unexplained_rounding_difference"


@pytest.mark.parametrize("amount,expected", [("0.03", "0.01"), ("-0.03", "-0.01")])
def test_half_up_uses_exact_tie_even_with_recurring_factor(amount, expected):
    assert Scale(Decimal(1), Decimal(6)).rounded(Decimal(amount)) == Decimal(expected)


def test_amplification_of_offsetting_credits_blocks_before_final_amounts():
    lines = [line(4, "100000.00", area="Area A"), line(5, "-99999.99", area="Area B")]
    result = decision(lines, Decimal("10000.00"))
    assert result["status"] == "blocked" and result["factor"] is None
    assert "display" not in result and "assignments" not in result
    assert result["issue"]["code"] == "implausible_factor"
    evidence = result["issue"]["evidence"]
    assert Decimal(evidence["proposed_factor"]) == Decimal("1000000")
    assert evidence["adjustable_base"] == "0.01"
    assert evidence["source_credit_magnitude"] == "99999.99"
    assert evidence["credit_rows"] == ["Trade Summary!D5"]

    ordinary = decision([line(4, "300"), line(5, "-200")], Decimal("101.00"))
    assert ordinary["status"] == "ready_for_workbook"


def test_automatic_residual_spills_in_stable_order_without_crossing_zero():
    protected = line(20, "100", trade="Make Good")
    lines = [line(row, "0.01", area=f"Area {row:02}") for row in range(4, 14)] + [protected]
    report = decision(list(reversed(lines)), Decimal("100.05"))
    assert report["status"] == "ready_for_workbook"
    assert Decimal(report["factor"]["decimal"]) == Decimal("0.5")
    rounding = report["display"]["rounding"]
    assert rounding["recipient"] is None
    assert rounding["recipients"] == [
        {"area": f"Area {row:02}", "trade": "Other", "amount": "-0.01"} for row in range(4, 9)
    ]
    assert rounding["residual_applied"] == "-0.05" and rounding["difference"] == "0.00"
    assert report["display"]["trade_totals"] == {"Make Good": "100.00", "Other": "0.05"}
    assert all(Decimal(cell["adjustable_display"]) >= 0 for cell in report["display"]["cells"])


def test_zero_factor_ignores_rounding_preference_without_a_residual():
    report = decision(
        [line(4, "10", trade="Make Good"), line(5, "1")], Decimal("10.00"),
        rounding_preference={"area": "Additional Items", "trade": "Other"},
    )
    assert report["status"] == "ready_for_workbook"
    assert Decimal(report["factor"]["decimal"]) == 0
    rounding = report["display"]["rounding"]
    assert rounding["residual_applied"] == "0.00" and rounding["recipients"] == []


def test_positive_residual_repairs_multiple_rounded_trade_totals():
    lines = [
        line(4, "-0.006", "Doors", "Area A"), line(5, "0.003", "Doors", "Area B"),
        line(6, "0.003", "Doors", "Area C"), line(7, "-0.006", "Other", "Area D"),
        line(8, "0.003", "Other", "Area E"), line(9, "0.003", "Other", "Area F"),
        line(10, "0.01", "Glazing", "Area G"),
    ]
    result = decision(lines, Decimal("0.01"))
    assert result["status"] == "ready_for_workbook"
    assert result["display"]["trade_totals"] == {"Doors": "0.00", "Glazing": "0.01", "Other": "0.00"}
    assert result["display"]["rounding"]["recipients"] == [
        {"area": "Area A", "trade": "Doors", "amount": "0.01"},
        {"area": "Area D", "trade": "Other", "amount": "0.01"},
    ]
