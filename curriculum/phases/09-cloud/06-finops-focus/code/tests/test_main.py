import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
MONTH = os.path.join(FIXTURES, "focus-month.csv")
CUR = os.path.join(FIXTURES, "cur-month.csv")
FSP = os.path.join(FIXTURES, "focus-spec-flexible-spend-plan-75pct.csv")
RESERVATION = os.path.join(FIXTURES, "focus-spec-reservation-partial-upfront.csv")
POLICY = os.path.join(FIXTURES, "tag-policy.json")
UNITS = os.path.join(FIXTURES, "units.json")
CE = os.path.join(FIXTURES, "ce-amortized-by-team.json")


def run(argv, stdin=""):
    out, err = StringIO(), StringIO()
    old = sys.stdin
    sys.stdin = StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def row(**fields):
    base = {
        "ChargeCategory": "Usage",
        "ChargePeriodStart": "2026-10-01T00:00:00Z",
        "ChargePeriodEnd": "2026-10-02T00:00:00Z",
        "BilledCost": "10.00",
        "EffectiveCost": "10.00",
        "ListCost": "10.00",
        "BillingCurrency": "USD",
        "ResourceId": "i-0test",
        "ServiceName": "Amazon Elastic Compute Cloud",
        "ServiceCategory": "Compute",
        "Tags": "",
    }
    base.update(fields)
    return base


def charges(*rows):
    columns = list(main.REQUIRED_COLUMNS) + ["ListCost", "Tags", "ResourceId"]
    return main.charges_from_rows(columns, rows)


def lines(allocation):
    return {name: str(amount) for name, amount in allocation.lines.items()}


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def policy():
    return main.load_tag_policy(load_json(POLICY), "team")


def by_id(stats, suffix):
    return next(s for s in stats if s.id.endswith(suffix))


class ParsingTests(unittest.TestCase):
    def test_decimals_and_timestamps_are_strict_and_errors_name_the_line(self):
        self.assertEqual(main.decimal_of("14.40", "BilledCost", 2), D("14.40"))
        self.assertEqual(main.decimal_of("", "ListCost", 2, default=D("1")), D("1"))
        for bad in ("12,50", "abc", "NaN", "Infinity"):
            with self.assertRaises(main.InputError) as caught:
                main.decimal_of(bad, "BilledCost", 7)
            self.assertIn("line 7", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.decimal_of("", "BilledCost", 3)
        self.assertEqual(main.time_of("2026-10-01T00:00:00Z", "ChargePeriodStart", 2).hour, 0)
        self.assertEqual(main.time_of("2026-10-01T02:00:00+02:00", "ChargePeriodStart", 2).hour, 0)
        with self.assertRaises(main.InputError):
            main.time_of("01/10/2026", "ChargePeriodStart", 2)

    def test_focus_reader_needs_the_cost_columns_and_points_cur_users_at_map_cur(self):
        self.assertEqual(len(main.load_focus(MONTH)), 198)
        with self.assertRaises(main.InputError) as caught:
            main.load_focus(CUR)
        self.assertIn("map-cur", str(caught.exception))
        self.assertIn("EffectiveCost", str(caught.exception))
        spec = main.load_focus(FSP)
        self.assertEqual({c.provider for c in spec}, {"Aura Web"})
        self.assertEqual(main.parse_tags('{"team": "search"}'), ({"team": "search"}, True))
        self.assertEqual(main.parse_tags("[1, 2]"), ({}, False))
        self.assertEqual(main.value({"Tags": "null"}, "Tags"), "")

    def test_one_currency_at_a_time(self):
        mixed = charges(row(), row(BillingCurrency="EUR"))
        with self.assertRaises(main.InputError) as caught:
            main.build_report(mixed)
        self.assertIn("EUR, USD", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.build_report([])


class RuleTests(unittest.TestCase):
    def test_rule_checks_catch_the_cost_rules_allocation_depends_on(self):
        rows = [
            row(ChargeCategory="Purchase", CommitmentDiscountId="sp-1", BilledCost="28.80", EffectiveCost="28.80"),
            row(CommitmentDiscountStatus="Used"),
            row(CommitmentDiscountId="sp-1", BilledCost="0", EffectiveCost="14.40"),
            row(BilledCost="10.00", EffectiveCost="9.00"),
            row(ChargeCategory="Tax", EffectiveCost="0.00"),
            row(CommitmentDiscountId="ri-1", CommitmentDiscountStatus="Unused", BilledCost="7.20", EffectiveCost="7.20"),
            row(CommitmentDiscountId="ri-1", CommitmentDiscountStatus="Wasted", BilledCost="0", EffectiveCost="7.20"),
            row(ChargeCategory="Discount"),
            row(Tags="team=search"),
            row(ChargePeriodEnd="2026-10-01T00:00:00Z"),
        ]
        columns = list(main.REQUIRED_COLUMNS) + ["ListCost", "Tags", "ResourceId", "CommitmentDiscountId", "CommitmentDiscountStatus"]
        found = main.check_rules(main.charges_from_rows(columns, rows))
        pairs = {(f.line, f.column) for f in found}
        self.assertIn((2, "EffectiveCost"), pairs)
        self.assertIn((3, "CommitmentDiscountStatus"), pairs)
        self.assertIn((4, "CommitmentDiscountStatus"), pairs)
        self.assertIn((5, "EffectiveCost"), pairs)
        self.assertIn((6, "EffectiveCost"), pairs)
        self.assertIn((7, "BilledCost"), pairs)
        self.assertIn((8, "CommitmentDiscountStatus"), pairs)
        self.assertIn((9, "ChargeCategory"), pairs)
        self.assertIn((10, "Tags"), pairs)
        self.assertIn((11, "ChargePeriodEnd"), pairs)
        self.assertEqual(main.check_rules(main.load_focus(MONTH)), [])
        self.assertEqual(main.check_rules(main.load_focus(FSP)), [])


class AllocationTests(unittest.TestCase):
    def test_month_showback_matches_the_hand_calculation_and_reconciles(self):
        allocation = main.allocate(main.load_focus(MONTH), "team", policy())
        self.assertEqual(lines(allocation), {
            "payments": "403.20",
            "platform": "369.60",
            "search": "252.00",
            main.UNTAGGED: "100.80",
            main.UNUSED: "100.80",
            main.SHARED: "52.48",
        })
        self.assertEqual(allocation.total, D("1278.88"))
        self.assertTrue(allocation.reconciles)
        self.assertEqual(allocation.ordered()[:3], ["payments", "platform", "search"])
        reasons = {u["source"]: u["reason"] for u in allocation.untagged}
        self.assertEqual(reasons["vol-0orphan01"], "value 'Platform' not allowed by the tag policy")
        self.assertEqual(reasons["DataTransfer-Out-Bytes"], "no team tag")
        self.assertEqual(allocation.untagged[0]["effective"], D("50.40"))

    def test_without_a_policy_a_capitalised_value_becomes_its_own_line(self):
        allocation = main.allocate(main.load_focus(MONTH), "team")
        self.assertEqual(allocation.lines["Platform"], D("33.60"))
        self.assertEqual(allocation.lines[main.UNTAGGED], D("67.20"))
        self.assertTrue(allocation.reconciles)

    def test_spreading_shared_cost_keeps_every_cent(self):
        allocation = main.allocate(main.load_focus(MONTH), "team", policy(), spread_shared=True)
        self.assertNotIn(main.UNUSED, allocation.lines)
        self.assertNotIn(main.SHARED, allocation.lines)
        self.assertEqual(allocation.lines[main.UNTAGGED], D("100.80"))
        self.assertEqual(sum(allocation.lines.values()), D("1278.88"))
        self.assertEqual(lines(allocation)["payments"], "463.51")
        self.assertEqual(allocation.direct["payments"], D("403.20"))

    def test_largest_remainder_rounding_gives_the_leftover_cents_to_the_biggest_fractions(self):
        third = D(1) / D(3)
        self.assertEqual(main.reconcile_cents({"a": third, "b": third, "c": third}, D("1.00")), {"a": D("0.34"), "b": D("0.33"), "c": D("0.33")})
        split = main.reconcile_cents({"x": D("10.005"), "y": D("-0.004"), "z": D("5.999")}, D("16.00"))
        self.assertEqual(sum(split.values()), D("16.00"))
        self.assertEqual(split, {"x": D("10.00"), "y": D("0.00"), "z": D("6.00")})

    def test_owner_rules_for_tags_commitments_and_shared_charges(self):
        unused = charges(row(CommitmentDiscountStatus="Unused"))[0]
        unused.commitment = "ri-1"
        self.assertEqual(main.owner(unused, "team"), (main.UNUSED, "unused commitment"))
        unused.tags = {"team": "platform"}
        self.assertEqual(main.owner(unused, "team"), ("platform", "tagged"))
        credit = charges(row(ChargeCategory="Credit", BilledCost="-5", EffectiveCost="-5"))[0]
        self.assertEqual(main.owner(credit, "team"), (main.SHARED, "credit"))
        variant = charges(row(Tags='{"Team": "search"}'))[0]
        self.assertEqual(main.owner(variant, "team"), (main.UNTAGGED, "key 'Team' is not 'team'"))
        valueless = charges(row(Tags='{"team": true}'))[0]
        self.assertEqual(main.owner(valueless, "team")[0], main.UNTAGGED)
        tagged_tax = charges(row(ChargeCategory="Tax", Tags='{"team": "search"}'))[0]
        self.assertEqual(main.owner(tagged_tax, "team"), ("search", "tagged"))

    def test_tag_policy_uses_the_assigned_key_and_star_wildcards(self):
        rule = main.load_tag_policy({"tags": {"costcenter": {"tag_key": {"@@assign": "CostCenter"}, "tag_value": {"@@assign": ["100", "2*"]}}}}, "costcenter")
        self.assertEqual(rule.key, "CostCenter")
        self.assertTrue(rule.allows("100"))
        self.assertTrue(rule.allows("2042"))
        self.assertFalse(rule.allows("1000"))
        self.assertFalse(rule.allows("x2"))
        self.assertTrue(main.load_tag_policy({"tags": {"env": {}}}, "env").allows("anything"))
        with self.assertRaises(main.InputError):
            main.load_tag_policy({"tags": {"env": {}}}, "team")
        with self.assertRaises(main.InputError):
            main.load_tag_policy(["not", "a", "policy"], "team")


class CommitmentTests(unittest.TestCase):
    def test_spec_flexible_spend_plan_at_75_percent(self):
        report = main.build_report(main.load_focus(FSP))
        self.assertEqual(report.billed, D("459024.00"))
        self.assertEqual(report.effective, D("1257.60"))
        plan = report.commitments[0]
        self.assertEqual(plan.utilisation, D("75"))
        self.assertEqual(plan.unused_effective, D("314.40"))
        self.assertEqual(plan.savings, D("471.60"))
        self.assertEqual(plan.net_savings, D("157.20"))
        self.assertEqual(plan.observed_hours, D("24"))
        self.assertEqual(plan.amortisation_gap, D("0"))
        compute = report.coverage[0]
        self.assertEqual(compute.coverage, D("100"))
        self.assertEqual(main.money(compute.esr), "11.11")

    def test_spec_reservation_partial_upfront_amortises_both_purchases(self):
        reservation = main.commitments(main.load_focus(RESERVATION))[0]
        self.assertEqual(reservation.purchases_billed, D("236884.68"))
        self.assertEqual(reservation.expected_effective, D("1205.52"))
        self.assertEqual(reservation.used_effective, D("1205.52"))
        self.assertEqual(reservation.utilisation, D("100"))
        self.assertEqual(reservation.unit, "Hours")

    def test_month_utilisation_coverage_and_the_upfront_paid_elsewhere(self):
        report = main.build_report(main.load_focus(MONTH))
        reservation = by_id(report.commitments, "ri-db-platform")
        self.assertEqual(reservation.utilisation, D("75"))
        self.assertEqual(reservation.unused_quantity, D("336"))
        self.assertEqual(reservation.amortisation_gap, D("201.60"))
        self.assertIn("paid outside this file", main.amortisation_note(reservation))
        plan = by_id(report.commitments, "sp-0compute")
        self.assertEqual(plan.utilisation, D("100"))
        self.assertEqual(plan.amortisation_gap, D("0"))
        cover = {c.category: (main.money(c.coverage), main.money(c.esr)) for c in report.coverage}
        self.assertEqual(cover["Compute"], ("80.00", "32.00"))
        self.assertEqual(cover["Databases"], ("100.00", "20.00"))
        self.assertEqual(cover["Storage"], ("0.00", "0.00"))

    def test_missing_unused_rows_inflate_utilisation_and_show_as_a_negative_gap(self):
        kept = [c for c in main.load_focus(FSP) if c.status != "Unused"]
        plan = main.commitments(kept)[0]
        self.assertEqual(plan.utilisation, D("100"))
        self.assertEqual(plan.amortisation_gap, D("0"))
        rows = [
            row(ChargeCategory="Purchase", CommitmentDiscountId="sp-1", BilledCost="28.80", EffectiveCost="0", ResourceId="sp-1"),
            row(CommitmentDiscountId="sp-1", CommitmentDiscountStatus="Used", BilledCost="0", EffectiveCost="20.00", CommitmentDiscountQuantity="20.00"),
        ]
        columns = list(main.REQUIRED_COLUMNS) + ["ListCost", "ResourceId", "CommitmentDiscountId", "CommitmentDiscountStatus", "CommitmentDiscountQuantity"]
        day = main.commitments(main.charges_from_rows(columns, rows))[0]
        self.assertEqual(day.utilisation, D("100"))
        self.assertEqual(day.amortisation_gap, D("-8.80"))
        self.assertIn("8.80 is missing", main.amortisation_note(day))

    def test_utilisation_falls_back_to_cost_when_a_quantity_is_missing(self):
        rows = [
            row(CommitmentDiscountId="sp-1", CommitmentDiscountStatus="Used", BilledCost="0", EffectiveCost="30"),
            row(CommitmentDiscountId="sp-1", CommitmentDiscountStatus="Unused", BilledCost="0", EffectiveCost="10", CommitmentDiscountQuantity="10"),
        ]
        columns = list(main.REQUIRED_COLUMNS) + ["ListCost", "CommitmentDiscountId", "CommitmentDiscountStatus", "CommitmentDiscountQuantity"]
        stats = main.commitments(main.charges_from_rows(columns, rows))[0]
        self.assertFalse(stats.quantity_complete)
        self.assertEqual(stats.utilisation, D("75"))


class CurTests(unittest.TestCase):
    def test_cur_mapping_reproduces_the_focus_fixture(self):
        rows, unblended, adjustments = main.map_cur(CUR)
        self.assertEqual(adjustments, {})
        self.assertEqual(len(rows), 198)
        self.assertEqual(sum(D(r["BilledCost"]) for r in rows), unblended)
        handle = io.StringIO()
        main.write_focus(rows, handle)
        mapped = main.charges_from_rows(list(main.FOCUS_COLUMNS), list(csv.DictReader(io.StringIO(handle.getvalue()))))
        month = main.load_focus(MONTH)
        self.assertEqual(main.check_rules(mapped), [])
        self.assertEqual(main.allocate(mapped, "team", policy()).lines, main.allocate(month, "team", policy()).lines)
        self.assertEqual(
            [(c.category, c.list_cost, c.covered_list, c.effective) for c in main.coverage(mapped)],
            [(c.category, c.list_cost, c.covered_list, c.effective) for c in main.coverage(month)],
        )
        mine = [(s.id, s.used_quantity, s.unused_quantity, s.unused_effective, s.amortisation_gap) for s in main.commitments(mapped)]
        theirs = [(s.id, s.used_quantity, s.unused_quantity, s.unused_effective, s.amortisation_gap) for s in main.commitments(month)]
        self.assertEqual(mine, theirs)

    def test_cur_line_item_types_map_onto_charge_categories(self):
        base = {"line_item_usage_start_date": "2026-10-01T00:00:00Z", "line_item_usage_end_date": "2026-10-02T00:00:00Z"}
        negation, note = main.map_cur_row(dict(base, line_item_line_item_type="SavingsPlanNegation", line_item_unblended_cost="-24"), 2)
        self.assertEqual((negation, note), ([], ""))
        fee, _ = main.map_cur_row(dict(base, line_item_line_item_type="RIFee", line_item_unblended_cost="14.40", line_item_usage_amount="96",
                                       reservation_reservation_a_r_n="ri-1", reservation_unused_quantity="24", reservation_unused_recurring_fee="3.60",
                                       reservation_unused_amortized_upfront_fee_for_billing_period="3.60"), 3)
        self.assertEqual([(r["ChargeCategory"], r["BilledCost"], r["EffectiveCost"], r["CommitmentDiscountStatus"]) for r in fee],
                         [("Purchase", "14.40", "0", ""), ("Usage", "0", "7.20", "Unused")])
        domain, _ = main.map_cur_row(dict(base, line_item_line_item_type="Fee", line_item_unblended_cost="15.00"), 4)
        self.assertEqual((domain[0]["ChargeCategory"], domain[0]["EffectiveCost"]), ("Purchase", "15.00"))
        edp, note = main.map_cur_row(dict(base, line_item_line_item_type="EdpDiscount", line_item_unblended_cost="-3.10"), 5)
        self.assertEqual((edp[0]["ChargeCategory"], note), ("Adjustment", "EdpDiscount"))
        spot, _ = main.map_cur_row(dict(base, line_item_line_item_type="Usage", line_item_unblended_cost="1", line_item_usage_type="SpotUsage:c7g.large",
                                        line_item_product_code="AmazonEC2"), 6)
        self.assertEqual((spot[0]["PricingCategory"], spot[0]["ServiceCategory"]), ("Dynamic", "Compute"))
        self.assertEqual(main.focus_tags('{"user_team": "search", "aws_createdBy": "x", "user_env": ""}', 7), '{"aws:createdBy":"x","team":"search"}')
        self.assertEqual(main.service_category("AmazonEC2", "EBS:VolumeUsage.gp3"), "Storage")
        self.assertEqual(main.service_category("AmazonEC2", "NatGateway-Bytes"), "Networking")
        self.assertEqual(main.service_category("AmazonSageMaker", "x"), "Other")
        with self.assertRaises(main.InputError):
            main.focus_tags("team=search", 8)


class AnomalyTests(unittest.TestCase):
    def spike(self, extra):
        month = main.load_focus(MONTH)
        template = next(c for c in month if c.resource == "i-0search01" and c.day == "2026-10-12")
        clone = main.Charge(**dict(template.__dict__))
        clone.resource, clone.billed, clone.effective, clone.list_cost = "i-0search-reindex", extra, extra, extra
        return month + [clone]

    def test_clean_month_has_no_anomalies_and_an_injected_spike_is_flagged_with_its_cause(self):
        days, found = main.find_anomalies(main.load_focus(MONTH), policy=policy())
        self.assertEqual(days, ["2026-10-%02d" % d for d in range(8, 15)])
        self.assertEqual(found, [])
        _, found = main.find_anomalies(self.spike(D("96.00")), policy=policy())
        self.assertEqual([(a.group, a.day, a.baseline, a.impact) for a in found], [("search", "2026-10-12", D("18.00"), D("96.00"))])
        self.assertEqual(found[0].contributors, [("i-0search-reindex", D("96.00"))])
        self.assertEqual(main.money(found[0].percent), "533.33")
        _, by_service = main.find_anomalies(self.spike(D("96.00")), by="service")
        self.assertEqual(by_service[0].group, "Amazon Elastic Compute Cloud")

    def test_both_thresholds_must_be_crossed(self):
        _, small = main.find_anomalies(self.spike(D("15.00")), policy=policy())
        self.assertEqual(small, [])
        _, relative = main.find_anomalies(self.spike(D("25.00")), policy=policy(), threshold_pct=D("200"))
        self.assertEqual(relative, [])
        _, flagged = main.find_anomalies(self.spike(D("25.00")), policy=policy(), threshold_pct=D("100"))
        self.assertEqual(len(flagged), 1)
        days, none = main.find_anomalies(main.load_focus(MONTH), window=30)
        self.assertEqual((days, none), ([], []))
        with self.assertRaises(main.InputError):
            main.find_anomalies(main.load_focus(MONTH), window=0)

    def test_new_spend_has_no_percentage(self):
        rows = [row(ChargePeriodStart=f"2026-10-0{d}T00:00:00Z", ChargePeriodEnd=f"2026-10-0{d + 1}T00:00:00Z", Tags='{"team": "search"}') for d in range(1, 5)]
        rows.append(row(ChargePeriodStart="2026-10-04T00:00:00Z", ChargePeriodEnd="2026-10-05T00:00:00Z", Tags='{"team": "ml"}', BilledCost="50", EffectiveCost="50", ResourceId="i-0gpu"))
        _, found = main.find_anomalies(charges(*rows), window=3)
        self.assertEqual([(a.group, a.percent) for a in found], [("ml", None)])


class ReconciliationTests(unittest.TestCase):
    def test_invoice_tolerance_follows_the_focus_formula(self):
        self.assertEqual(main.invoice_tolerance(5, "USD"), D("1.00"))
        self.assertEqual(main.invoice_tolerance(1000000, "USD"), D("5.000"))
        self.assertEqual(main.invoice_tolerance(5, "JPY"), D("100"))
        month = main.load_focus(MONTH)
        [ok] = main.check_invoices(month, [("INV-2026-10-0001", D("1077.90"))], "USD")
        self.assertTrue(ok["ok"])
        [bad] = main.check_invoices(month, [("INV-2026-10-0001", D("1082.28"))], "USD")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["difference"], D("-5.00"))
        [missing] = main.check_invoices(month, [("INV-OTHER", D("0"))], "USD")
        self.assertFalse(missing["ok"])

    def test_unit_costs_divide_the_reconciled_lines(self):
        report = main.build_report(main.load_focus(MONTH), policy=policy(), units=load_json(UNITS))
        costs = {u["line"]: u["unit_cost"] for u in report.unit_costs}
        self.assertEqual(costs, {"payments": D("0.48"), "platform": D("0.1"), "search": D("0.1")})
        for bad in ({"units": {"search": {"quantity": 0}}}, {"units": {"search": {"quantity": "many"}}}, {"teams": {}}):
            with self.assertRaises(main.InputError):
                main.unit_costs(report.allocation, bad)

    def test_tag_audit_ranks_resources_to_fix_by_cost(self):
        resources = main.load_resources({"ResourceTagMappingList": [
            {"ResourceARN": "arn:aws:ec2:us-east-1:222222222222:volume/vol-0orphan01", "Tags": [{"Key": "team", "Value": "Platform"}]},
            {"ResourceARN": "arn:aws:ec2:us-east-1:222222222222:instance/i-0search01", "Tags": [{"Key": "team", "Value": "search"}]},
            {"ResourceARN": "arn:aws:elasticloadbalancing:us-east-1:333333333333:loadbalancer/app/legacy-web/0a1b2c3d4e5f", "Tags": [{"Key": "Name", "Value": "legacy"}]},
        ]})
        audit = main.tag_audit(resources, "team", policy(), main.load_focus(MONTH))
        self.assertEqual([(a["ok"], str(a["effective"])) for a in audit], [(False, "50.40"), (False, "33.60"), (True, "168.00")])
        self.assertEqual(audit[1]["status"], "value 'Platform' not allowed by the tag policy")
        self.assertTrue(main.matches_resource("arn:aws:rds:us-east-1:1:db:platform-db-1", "arn:aws:rds:us-east-1:1:db:platform-db-1"))
        self.assertFalse(main.matches_resource("arn:aws:ec2:us-east-1:1:instance/i-0search01", "i-0search0"))
        with self.assertRaises(main.InputError):
            main.load_resources({"Resources": []})

    def test_cost_explorer_groups_compare_to_the_cent(self):
        rows, _, _ = main.map_cur(CUR)
        handle = io.StringIO()
        main.write_focus(rows, handle)
        mapped = main.charges_from_rows(list(main.FOCUS_COLUMNS), list(csv.DictReader(io.StringIO(handle.getvalue()))))
        doc = load_json(CE)
        diff = main.ce_diff(mapped, doc)
        self.assertTrue(all(r["ok"] for r in diff))
        self.assertEqual({r["group"] for r in diff}, {"team$", "team$Platform", "team$payments", "team$platform", "team$search"})
        doc["ResultsByTime"][0]["Groups"][4]["Metrics"]["AmortizedCost"]["Amount"] = "250.00"
        changed = {r["group"]: r for r in main.ce_diff(mapped, doc)}
        self.assertEqual(changed["team$search"]["difference"], D("2.00"))
        with self.assertRaises(main.InputError):
            main.ce_diff(mapped, {"ResultsByTime": [{"Groups": [{"Keys": ["Amazon EC2"], "Metrics": {}}]}]})
        with self.assertRaises(main.InputError):
            main.ce_diff(mapped, load_json(CE), metric="UnblendedCost")


class CliTests(unittest.TestCase):
    def test_report_text_json_and_exit_codes(self):
        code, out, _ = run(["report", MONTH, "--tag-policy", POLICY, "--invoice", "INV-2026-10-0001=1077.28"])
        self.assertEqual(code, 0)
        self.assertIn("reconciles with the EffectiveCost total 1278.88", out)
        self.assertIn("201.60 was paid outside this file", out)
        code, out, _ = run(["report", MONTH, "--json", "--units", UNITS])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["totals"], {"billed": "1077.28", "effective": "1278.88", "on_demand_equivalent": "1596.00"})
        self.assertEqual(doc["allocation"]["sum"], doc["allocation"]["total"])
        self.assertEqual(doc["unit_costs"][0]["unit_cost"], "0.48")
        code, out, _ = run(["report", MONTH, "--invoice", "INV-2026-10-0001=1100.00"])
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)
        code, _, err = run(["report", CUR])
        self.assertEqual(code, 2)
        self.assertIn("map-cur", err)
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main.main(["report", MONTH, "--invoice", "no-amount"])
        self.assertEqual(caught.exception.code, 2)

    def test_map_cur_anomalies_tags_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "focus.csv")
            code, _, err = run(["map-cur", CUR, "-o", out_path])
            self.assertEqual(code, 0)
            self.assertIn("1077.28 = sum of line_item_unblended_cost 1077.28", err)
            code, out, _ = run(["report", "-", "--json"], stdin=read(out_path))
            self.assertEqual((code, json.loads(out)["allocation"]["total"]), (0, "1278.88"))
            code, out, _ = run(["anomalies", out_path, "--json"])
            self.assertEqual((code, json.loads(out)["anomalies"]), (0, []))
            resources = os.path.join(tmp, "resources.json")
            with open(resources, "w", encoding="utf-8") as handle:
                json.dump({"ResourceTagMappingList": [{"ResourceARN": "arn:aws:ec2:us-east-1:2:instance/i-0legacy", "Tags": []}]}, handle)
            code, out, _ = run(["tags", "--resources", resources, "--tag-policy", POLICY])
            self.assertEqual(code, 1)
            self.assertIn("FIX", out)
            code, out, _ = run(["ce-diff", out_path, "--cost-explorer", CE])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
