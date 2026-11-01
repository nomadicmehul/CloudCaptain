"""billjoin: turn cost and usage rows in the FinOps FOCUS format (or a CUR 2.0 style export mapped onto it) into a showback report: EffectiveCost allocated by tag with untagged, unused-commitment and shared lines that reconcile to the cent, commitment utilisation, coverage and amortisation, unit costs, invoice checks and daily anomalies against a rolling baseline."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")
CHARGE_CATEGORIES = ("Usage", "Purchase", "Tax", "Credit", "Adjustment")
COMMITMENT_STATUSES = ("Used", "Unused")
SHARED_CATEGORIES = ("Tax", "Credit", "Adjustment")
REQUIRED_COLUMNS = ("ChargeCategory", "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency")
NULLS = ("", "null", "NULL")
UNTAGGED = "(untagged)"
UNUSED = "(unused commitment)"
SHARED = "(shared)"
SPECIAL_LINES = (UNTAGGED, UNUSED, SHARED)
CURRENCY_SUBUNITS = {"JPY": Decimal("1"), "KRW": Decimal("1"), "CLP": Decimal("1"), "ISK": Decimal("1")}
GROUPINGS = ("tag", "service", "account")
CE_METRICS = {"AmortizedCost": "effective", "NetAmortizedCost": "effective", "UnblendedCost": "billed", "NetUnblendedCost": "billed"}
FOCUS_COLUMNS = (
    "BillingAccountId", "SubAccountId", "BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart", "ChargePeriodEnd",
    "ChargeCategory", "ChargeClass", "ChargeFrequency", "ChargeDescription", "InvoiceId", "InvoiceIssuerName",
    "ServiceProviderName", "ServiceName", "ServiceCategory", "RegionId", "ResourceId", "PricingCategory",
    "ConsumedQuantity", "ConsumedUnit", "ListCost", "BilledCost", "EffectiveCost", "BillingCurrency",
    "CommitmentDiscountId", "CommitmentDiscountCategory", "CommitmentDiscountType", "CommitmentDiscountStatus",
    "CommitmentDiscountQuantity", "CommitmentDiscountUnit", "Tags", "x_ServiceCode", "x_UsageType", "x_Operation",
)
CUR_REQUIRED = ("line_item_line_item_type", "line_item_usage_start_date", "line_item_usage_end_date", "line_item_unblended_cost")
SERVICE_NAMES = {
    "AmazonEC2": "Amazon Elastic Compute Cloud",
    "AmazonRDS": "Amazon Relational Database Service",
    "AmazonS3": "Amazon Simple Storage Service",
    "AWSELB": "Elastic Load Balancing",
    "AWSDataTransfer": "AWS Data Transfer",
    "AmazonVPC": "Amazon Virtual Private Cloud",
    "AWSLambda": "AWS Lambda",
    "ComputeSavingsPlans": "Savings Plans for AWS Compute usage",
}
PRODUCT_CATEGORIES = {
    "AmazonEC2": "Compute",
    "AWSLambda": "Compute",
    "AmazonECS": "Compute",
    "AmazonEKS": "Compute",
    "ComputeSavingsPlans": "Compute",
    "AmazonRDS": "Databases",
    "AmazonDynamoDB": "Databases",
    "AmazonS3": "Storage",
    "AmazonEFS": "Storage",
    "AWSELB": "Networking",
    "AmazonVPC": "Networking",
    "AmazonCloudFront": "Networking",
    "AWSDataTransfer": "Networking",
}
USAGE_TYPE_CATEGORIES = (("EBS:", "Storage"), ("NatGateway", "Networking"), ("DataTransfer", "Networking"), ("LoadBalancer", "Networking"))


class InputError(ValueError):
    pass


@dataclass
class Charge:
    line: int
    category: str
    start_at: datetime
    end_at: datetime
    billed: Decimal
    effective: Decimal
    list_cost: Decimal
    currency: str
    provider: str = ""
    service: str = ""
    service_category: str = ""
    resource: str = ""
    description: str = ""
    sub_account: str = ""
    invoice: str = ""
    pricing_category: str = ""
    commitment: str = ""
    commitment_category: str = ""
    commitment_type: str = ""
    status: str = ""
    commitment_quantity: Optional[Decimal] = None
    commitment_unit: str = ""
    tags: Dict[str, Any] = field(default_factory=dict)
    tags_valid: bool = True

    @property
    def day(self) -> str:
        return self.start_at.strftime("%Y-%m-%d")

    @property
    def source(self) -> str:
        return self.resource or self.description or self.service or f"line {self.line}"


@dataclass
class Finding:
    line: int
    column: str
    rule: str


@dataclass
class TagPolicy:
    key: str
    allowed: List[str]

    def allows(self, text: str) -> bool:
        return not self.allowed or any(re.fullmatch(re.escape(p).replace(r"\*", ".*"), text) for p in self.allowed)


@dataclass
class Allocation:
    key: str
    lines: Dict[str, Decimal]
    direct: Dict[str, Decimal]
    total: Decimal
    spread: bool
    untagged: List[Dict[str, Any]]

    @property
    def reconciles(self) -> bool:
        return sum(self.lines.values(), ZERO) == self.total

    def ordered(self) -> List[str]:
        teams = sorted((n for n in self.lines if n not in SPECIAL_LINES), key=lambda n: (-self.lines[n], n))
        return teams + [n for n in SPECIAL_LINES if n in self.lines]


@dataclass
class CommitmentStats:
    id: str
    type: str = ""
    category: str = ""
    unit: str = ""
    purchases_billed: Decimal = ZERO
    used_quantity: Decimal = ZERO
    unused_quantity: Decimal = ZERO
    quantity_complete: bool = True
    used_effective: Decimal = ZERO
    unused_effective: Decimal = ZERO
    covered_list: Decimal = ZERO
    observed_hours: Decimal = ZERO
    expected_effective: Decimal = ZERO

    @property
    def utilisation(self) -> Optional[Decimal]:
        quantity = self.used_quantity + self.unused_quantity
        if self.quantity_complete and quantity > 0:
            return HUNDRED * self.used_quantity / quantity
        cost = self.used_effective + self.unused_effective
        return HUNDRED * self.used_effective / cost if cost else None

    @property
    def savings(self) -> Decimal:
        return self.covered_list - self.used_effective

    @property
    def net_savings(self) -> Decimal:
        return self.savings - self.unused_effective

    @property
    def amortisation_gap(self) -> Decimal:
        return self.used_effective + self.unused_effective - self.expected_effective


@dataclass
class Coverage:
    category: str
    list_cost: Decimal = ZERO
    covered_list: Decimal = ZERO
    effective: Decimal = ZERO

    @property
    def coverage(self) -> Decimal:
        return HUNDRED * self.covered_list / self.list_cost if self.list_cost else ZERO

    @property
    def savings(self) -> Decimal:
        return self.list_cost - self.effective

    @property
    def esr(self) -> Decimal:
        return HUNDRED * self.savings / self.list_cost if self.list_cost else ZERO


@dataclass
class Report:
    rows: int
    currency: str
    providers: List[str]
    first_day: str
    last_day: str
    billed: Decimal
    effective: Decimal
    list_usage: Decimal
    findings: List[Finding]
    allocation: Allocation
    commitments: List[CommitmentStats]
    coverage: List[Coverage]
    unit_costs: List[Dict[str, Any]]
    invoices: List[Dict[str, Any]]

    @property
    def ok(self) -> bool:
        return not self.findings and self.allocation.reconciles and all(i["ok"] for i in self.invoices)


@dataclass
class Anomaly:
    group: str
    day: str
    actual: Decimal
    baseline: Decimal
    impact: Decimal
    percent: Optional[Decimal]
    contributors: List[Tuple[str, Decimal]]


def money(amount: Decimal) -> str:
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def quantity_text(amount: Decimal) -> str:
    text = format(amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize(), "f")
    return "0" if text in ("-0", "") else text


def value(row: Dict[str, Optional[str]], column: str) -> str:
    raw = row.get(column)
    text = (raw or "").strip()
    return "" if text in NULLS else text


def decimal_of(text: str, column: str, line: int, default: Optional[Decimal] = None) -> Decimal:
    if text == "":
        if default is None:
            raise InputError(f"line {line}: {column} is empty; it must be a decimal")
        return default
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise InputError(f"line {line}: {column} is not a decimal: {text!r}")
    if not number.is_finite():
        raise InputError(f"line {line}: {column} is not a finite decimal: {text!r}")
    return number


def time_of(text: str, column: str, line: int) -> datetime:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        raise InputError(f"line {line}: {column} is not an ISO 8601 date-time: {text!r} (example 2026-10-01T00:00:00Z)")
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def hours_between(start: datetime, end: datetime) -> Decimal:
    return Decimal(int((end - start).total_seconds())) / Decimal(3600)


def parse_tags(text: str) -> Tuple[Dict[str, Any], bool]:
    if text == "":
        return {}, True
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}, False
    return (doc, True) if isinstance(doc, dict) else ({}, False)


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")


def read_csv(path: str) -> Tuple[List[str], List[Dict[str, Optional[str]]]]:
    reader = csv.DictReader(io.StringIO(read_text(path)))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def read_json(path: str) -> Any:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON: {exc}")


def charge_of(row: Dict[str, Optional[str]], line: int) -> Charge:
    tags, tags_valid = parse_tags(value(row, "Tags"))
    billed = decimal_of(value(row, "BilledCost"), "BilledCost", line)
    quantity = value(row, "CommitmentDiscountQuantity")
    return Charge(
        line=line,
        category=value(row, "ChargeCategory"),
        start_at=time_of(value(row, "ChargePeriodStart"), "ChargePeriodStart", line),
        end_at=time_of(value(row, "ChargePeriodEnd"), "ChargePeriodEnd", line),
        billed=billed,
        effective=decimal_of(value(row, "EffectiveCost"), "EffectiveCost", line),
        list_cost=decimal_of(value(row, "ListCost"), "ListCost", line, default=billed),
        currency=value(row, "BillingCurrency"),
        provider=value(row, "ServiceProviderName") or value(row, "ProviderName"),
        service=value(row, "ServiceName"),
        service_category=value(row, "ServiceCategory"),
        resource=value(row, "ResourceId"),
        description=value(row, "ChargeDescription"),
        sub_account=value(row, "SubAccountId"),
        invoice=value(row, "InvoiceId"),
        pricing_category=value(row, "PricingCategory"),
        commitment=value(row, "CommitmentDiscountId"),
        commitment_category=value(row, "CommitmentDiscountCategory"),
        commitment_type=value(row, "CommitmentDiscountType"),
        status=value(row, "CommitmentDiscountStatus"),
        commitment_quantity=decimal_of(quantity, "CommitmentDiscountQuantity", line) if quantity else None,
        commitment_unit=value(row, "CommitmentDiscountUnit"),
        tags=tags,
        tags_valid=tags_valid,
    )


def charges_from_rows(columns: Sequence[str], rows: Iterable[Dict[str, Optional[str]]], name: str = "input") -> List[Charge]:
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        hint = "; this is a CUR export, convert it with billjoin map-cur first" if "line_item_line_item_type" in columns else ""
        raise InputError(f"{name}: missing FOCUS column(s) {', '.join(missing)}{hint}")
    return [charge_of(row, number) for number, row in enumerate(rows, start=2)]


def load_focus(path: str) -> List[Charge]:
    columns, rows = read_csv(path)
    return charges_from_rows(columns, rows, path)


def rule_findings(charge: Charge) -> List[Finding]:
    found: List[Tuple[str, str]] = []
    if charge.category not in CHARGE_CATEGORIES:
        found.append(("ChargeCategory", f"{charge.category!r} is not one of {', '.join(CHARGE_CATEGORIES)}"))
    if charge.status and charge.status not in COMMITMENT_STATUSES:
        found.append(("CommitmentDiscountStatus", f"{charge.status!r} is not Used or Unused"))
    if charge.status and not charge.commitment:
        found.append(("CommitmentDiscountStatus", "MUST be null when CommitmentDiscountId is null"))
    if charge.commitment and charge.category == "Usage" and not charge.status:
        found.append(("CommitmentDiscountStatus", "MUST NOT be null when CommitmentDiscountId is set and ChargeCategory is Usage"))
    if charge.category == "Purchase" and charge.commitment and charge.effective != 0:
        found.append(("EffectiveCost", "MUST be 0 for a Purchase that covers other charges; the Used and Unused rows carry its cost"))
    if charge.category == "Usage" and not charge.commitment and charge.effective != charge.billed:
        found.append(("EffectiveCost", "MUST equal BilledCost for Usage that no commitment covers"))
    if charge.category in ("Tax", "Credit") and charge.effective != charge.billed:
        found.append(("EffectiveCost", "MUST equal BilledCost when ChargeCategory is Tax or Credit"))
    if charge.status == "Unused" and charge.billed != 0:
        found.append(("BilledCost", "an Unused row was paid for by its purchase, so its BilledCost is 0"))
    if charge.end_at <= charge.start_at:
        found.append(("ChargePeriodEnd", "must be after ChargePeriodStart (the end is exclusive)"))
    if not charge.tags_valid:
        found.append(("Tags", "MUST be a JSON object"))
    return [Finding(charge.line, column, rule) for column, rule in found]


def check_rules(charges: Iterable[Charge]) -> List[Finding]:
    return [finding for charge in charges for finding in rule_findings(charge)]


def load_tag_policy(doc: Any, key: str) -> TagPolicy:
    rules = doc.get("tags") if isinstance(doc, dict) else None
    if not isinstance(rules, dict):
        raise InputError('tag policy: expected an AWS Organizations tag policy, {"tags": {"KEY": {"tag_key": {"@@assign": "KEY"}, "tag_value": {"@@assign": [...]}}}}')
    for name, rule in rules.items():
        assigned = ((rule or {}).get("tag_key") or {}).get("@@assign") or name
        if str(assigned).lower() == key.lower():
            allowed = ((rule or {}).get("tag_value") or {}).get("@@assign") or []
            return TagPolicy(str(assigned), [str(v) for v in allowed])
    raise InputError(f"tag policy has no rule for the key {key!r} (it has: {', '.join(sorted(rules)) or 'none'})")


def tag_value(tags: Dict[str, Any], key: str) -> Tuple[Optional[str], str]:
    if key in tags:
        raw = tags[key]
        if raw is None or raw == "":
            return None, f"empty {key} tag"
        if raw is True:
            return None, f"{key} tag without a value"
        return str(raw), "tagged"
    for other in tags:
        if other.lower() == key.lower():
            return None, f"key {other!r} is not {key!r}"
    return None, ""


def owner(charge: Charge, key: str, policy: Optional[TagPolicy] = None) -> Tuple[str, str]:
    found, reason = tag_value(charge.tags, key)
    if found is not None:
        if policy is None or policy.allows(found):
            return found, "tagged"
        return UNTAGGED, f"value {found!r} not allowed by the tag policy"
    if reason:
        return UNTAGGED, reason
    if charge.status == "Unused":
        return UNUSED, "unused commitment"
    if charge.category in SHARED_CATEGORIES:
        return SHARED, charge.category.lower()
    return UNTAGGED, f"no {key} tag"


def reconcile_cents(values: Dict[str, Decimal], total: Decimal) -> Dict[str, Decimal]:
    floors = {name: amount.quantize(CENT, rounding=ROUND_FLOOR) for name, amount in values.items()}
    leftover = int((total - sum(floors.values(), ZERO)) / CENT)
    order = sorted(values, key=lambda name: (-(values[name] - floors[name]), name))
    for name in order[: max(0, min(leftover, len(order)))]:
        floors[name] += CENT
    return floors


def allocate(charges: Sequence[Charge], key: str = "team", policy: Optional[TagPolicy] = None, spread_shared: bool = False) -> Allocation:
    if policy is not None:
        key = policy.key
    exact: Dict[str, Decimal] = {}
    sources: Dict[Tuple[str, str], Decimal] = {}
    for charge in charges:
        if charge.effective == 0:
            continue
        line, reason = owner(charge, key, policy)
        exact[line] = exact.get(line, ZERO) + charge.effective
        if line == UNTAGGED:
            sources[(charge.source, reason)] = sources.get((charge.source, reason), ZERO) + charge.effective
    total = sum((c.effective for c in charges), ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
    direct = {name: amount.quantize(CENT, rounding=ROUND_HALF_UP) for name, amount in exact.items()}
    teams = {name: amount for name, amount in exact.items() if name not in SPECIAL_LINES}
    base = sum(teams.values(), ZERO)
    if spread_shared and base > 0:
        pool = exact.pop(UNUSED, ZERO) + exact.pop(SHARED, ZERO)
        for name, amount in teams.items():
            exact[name] = amount + pool * amount / base
    untagged = [
        {"source": source, "reason": reason, "effective": amount}
        for (source, reason), amount in sorted(sources.items(), key=lambda item: (-item[1], item[0]))
    ]
    return Allocation(key, reconcile_cents(exact, total), direct, total, spread_shared and base > 0, untagged)


def hourly_rate(purchases: Iterable[Charge], start: datetime, end: datetime) -> Decimal:
    rate = ZERO
    for purchase in purchases:
        span = hours_between(purchase.start_at, purchase.end_at)
        if span > 0 and purchase.start_at <= start and end <= purchase.end_at:
            rate += purchase.billed / span
    return rate


def commitments(charges: Iterable[Charge]) -> List[CommitmentStats]:
    stats: Dict[str, CommitmentStats] = {}
    purchases: Dict[str, List[Charge]] = {}
    periods: Dict[str, Set[Tuple[datetime, datetime]]] = {}
    for charge in charges:
        if not charge.commitment:
            continue
        entry = stats.setdefault(charge.commitment, CommitmentStats(charge.commitment))
        entry.type = entry.type or charge.commitment_type
        entry.category = entry.category or charge.commitment_category
        entry.unit = entry.unit or charge.commitment_unit
        if charge.category == "Purchase":
            entry.purchases_billed += charge.billed
            purchases.setdefault(charge.commitment, []).append(charge)
            continue
        if charge.status not in COMMITMENT_STATUSES:
            continue
        if charge.commitment_quantity is None:
            entry.quantity_complete = False
        amount = charge.commitment_quantity or ZERO
        if charge.status == "Used":
            entry.used_quantity += amount
            entry.used_effective += charge.effective
            entry.covered_list += charge.list_cost
        else:
            entry.unused_quantity += amount
            entry.unused_effective += charge.effective
        periods.setdefault(charge.commitment, set()).add((charge.start_at, charge.end_at))
    for commitment_id, entry in stats.items():
        for start, end in periods.get(commitment_id, set()):
            hours = hours_between(start, end)
            entry.observed_hours += hours
            entry.expected_effective += hours * hourly_rate(purchases.get(commitment_id, []), start, end)
    return sorted(stats.values(), key=lambda s: s.id)


def coverage(charges: Iterable[Charge]) -> List[Coverage]:
    by_category: Dict[str, Coverage] = {}
    for charge in charges:
        if charge.category != "Usage":
            continue
        entry = by_category.setdefault(charge.service_category or "(none)", Coverage(charge.service_category or "(none)"))
        entry.effective += charge.effective
        if charge.status == "Unused":
            continue
        entry.list_cost += charge.list_cost
        if charge.status == "Used":
            entry.covered_list += charge.list_cost
    return sorted(by_category.values(), key=lambda c: (-c.list_cost, c.category))


def unit_costs(allocation: Allocation, doc: Any) -> List[Dict[str, Any]]:
    entries = doc.get("units") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        raise InputError('units file: expected {"units": {"LINE": {"unit": "1k orders", "quantity": 840}}}')
    rows = []
    for name, spec in sorted(entries.items()):
        try:
            amount = Decimal(str((spec or {}).get("quantity")))
        except InvalidOperation:
            raise InputError(f"units file: {name}.quantity is not a number")
        if not amount.is_finite() or amount <= 0:
            raise InputError(f"units file: {name}.quantity must be a positive number")
        cost = allocation.lines.get(name)
        rows.append({
            "line": name,
            "unit": str((spec or {}).get("unit") or "unit"),
            "quantity": amount,
            "cost": cost,
            "unit_cost": None if cost is None else cost / amount,
        })
    return rows


def parse_invoice(text: str) -> Tuple[str, Decimal]:
    invoice_id, sep, amount = text.rpartition("=")
    try:
        number = Decimal(amount)
    except InvalidOperation:
        number = Decimal("NaN")
    if not sep or not invoice_id or not number.is_finite():
        raise argparse.ArgumentTypeError(f"expected INVOICE_ID=AMOUNT, got {text!r}")
    return invoice_id, number


def invoice_tolerance(rows: int, currency: str) -> Decimal:
    subunit = CURRENCY_SUBUNITS.get(currency, CENT)
    return max(HUNDRED * subunit, Decimal(rows).sqrt() * Decimal("0.5") * subunit)


def check_invoices(charges: Sequence[Charge], expected: Sequence[Tuple[str, Decimal]], currency: str) -> List[Dict[str, Any]]:
    results = []
    for invoice_id, amount in expected:
        rows = [c for c in charges if c.invoice == invoice_id]
        billed = sum((c.billed for c in rows), ZERO)
        tolerance = invoice_tolerance(len(rows), currency)
        difference = billed - amount
        results.append({
            "invoice": invoice_id,
            "rows": len(rows),
            "billed": billed,
            "invoice_total": amount,
            "difference": difference,
            "tolerance": tolerance,
            "ok": bool(rows) and abs(difference) <= tolerance,
        })
    return results


def single_currency(charges: Sequence[Charge]) -> str:
    if not charges:
        raise InputError("no cost rows in the input")
    currencies = sorted({c.currency for c in charges})
    if len(currencies) != 1:
        raise InputError(f"more than one BillingCurrency ({', '.join(c or 'empty' for c in currencies)}); billjoin sums one currency at a time, so filter the file first")
    return currencies[0]


def build_report(
    charges: Sequence[Charge],
    key: str = "team",
    policy: Optional[TagPolicy] = None,
    spread_shared: bool = False,
    units: Any = None,
    invoices: Sequence[Tuple[str, Decimal]] = (),
) -> Report:
    currency = single_currency(charges)
    allocation = allocate(charges, key, policy, spread_shared)
    return Report(
        rows=len(charges),
        currency=currency,
        providers=sorted({c.provider for c in charges if c.provider}),
        first_day=min(c.day for c in charges),
        last_day=max(c.end_at for c in charges).strftime("%Y-%m-%d"),
        billed=sum((c.billed for c in charges), ZERO),
        effective=sum((c.effective for c in charges), ZERO),
        list_usage=sum((c.list_cost for c in charges if c.category == "Usage" and c.status != "Unused"), ZERO),
        findings=check_rules(charges),
        allocation=allocation,
        commitments=commitments(charges),
        coverage=coverage(charges),
        unit_costs=unit_costs(allocation, units) if units is not None else [],
        invoices=check_invoices(charges, invoices, currency),
    )


def group_of(charge: Charge, by: str, key: str, policy: Optional[TagPolicy]) -> str:
    if by == "service":
        return charge.service or "(no service)"
    if by == "account":
        return charge.sub_account or "(no sub account)"
    return owner(charge, policy.key if policy else key, policy)[0]


def contributors(by_day: Dict[str, Dict[str, Decimal]], day: str, history: Sequence[str], limit: int = 3) -> List[Tuple[str, Decimal]]:
    sources = set(by_day.get(day, {})) | {s for d in history for s in by_day.get(d, {})}
    deltas = []
    for source in sources:
        typical = statistics.median([by_day.get(d, {}).get(source, ZERO) for d in history])
        delta = by_day.get(day, {}).get(source, ZERO) - typical
        if delta > 0:
            deltas.append((source, delta))
    return sorted(deltas, key=lambda item: (-item[1], item[0]))[:limit]


def find_anomalies(
    charges: Sequence[Charge],
    by: str = "tag",
    key: str = "team",
    policy: Optional[TagPolicy] = None,
    window: int = 7,
    threshold_pct: Decimal = Decimal("25"),
    min_impact: Decimal = Decimal("20"),
) -> Tuple[List[str], List[Anomaly]]:
    if window < 1:
        raise InputError("--window must be at least 1 day")
    usage = [c for c in charges if c.category == "Usage"]
    days = sorted({c.day for c in usage})
    series: Dict[str, Dict[str, Dict[str, Decimal]]] = {}
    for charge in usage:
        per_day = series.setdefault(group_of(charge, by, key, policy), {}).setdefault(charge.day, {})
        per_day[charge.source] = per_day.get(charge.source, ZERO) + charge.effective
    found = []
    for group in sorted(series):
        by_day = series[group]
        totals = {d: sum(by_day.get(d, {}).values(), ZERO) for d in days}
        for index in range(window, len(days)):
            day, history = days[index], days[index - window:index]
            baseline = statistics.median([totals[d] for d in history])
            impact = totals[day] - baseline
            if impact < min_impact or (baseline > 0 and impact * HUNDRED < threshold_pct * baseline):
                continue
            percent = HUNDRED * impact / baseline if baseline > 0 else None
            found.append(Anomaly(group, day, totals[day], baseline, impact, percent, contributors(by_day, day, history)))
    return days[window:], found


def service_category(product: str, usage_type: str) -> str:
    for marker, category in USAGE_TYPE_CATEGORIES:
        if marker in usage_type:
            return category
    return PRODUCT_CATEGORIES.get(product, "Other")


def focus_tags(text: str, line: int) -> str:
    if text == "":
        return ""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise InputError(f"line {line}: resource_tags is not a JSON object: {text!r}")
    if not isinstance(doc, dict):
        raise InputError(f"line {line}: resource_tags is not a JSON object: {text!r}")
    tags = {}
    for name, tag in doc.items():
        if tag in (None, ""):
            continue
        if name.startswith("user_"):
            tags[name[len("user_"):]] = tag
        elif name.startswith("aws_"):
            tags["aws:" + name[len("aws_"):]] = tag
        else:
            tags[name] = tag
    return json.dumps(tags, sort_keys=True, separators=(",", ":")) if tags else ""


def text_of(number: Optional[Decimal]) -> str:
    return "" if number is None else format(number, "f")


def map_cur_row(row: Dict[str, Optional[str]], line: int) -> Tuple[List[Dict[str, str]], str]:
    def amount(column: str, default: Optional[Decimal] = None) -> Decimal:
        return decimal_of(value(row, column), column, line, default)

    kind = value(row, "line_item_line_item_type")
    unblended = amount("line_item_unblended_cost")
    public = amount("pricing_public_on_demand_cost", unblended)
    usage_amount = amount("line_item_usage_amount", ZERO)
    product, usage_type = value(row, "line_item_product_code"), value(row, "line_item_usage_type")
    base = {column: "" for column in FOCUS_COLUMNS}
    base.update({
        "BillingAccountId": value(row, "bill_payer_account_id"),
        "SubAccountId": value(row, "line_item_usage_account_id"),
        "BillingPeriodStart": value(row, "bill_billing_period_start_date"),
        "BillingPeriodEnd": value(row, "bill_billing_period_end_date"),
        "ChargePeriodStart": value(row, "line_item_usage_start_date"),
        "ChargePeriodEnd": value(row, "line_item_usage_end_date"),
        "ChargeDescription": value(row, "line_item_line_item_description"),
        "InvoiceId": value(row, "bill_invoice_id"),
        "InvoiceIssuerName": value(row, "bill_billing_entity") or "AWS",
        "ServiceProviderName": "AWS",
        "ServiceName": SERVICE_NAMES.get(product, product),
        "ServiceCategory": service_category(product, usage_type),
        "RegionId": value(row, "product_region_code"),
        "ResourceId": value(row, "line_item_resource_id"),
        "BillingCurrency": value(row, "line_item_currency_code") or "USD",
        "Tags": focus_tags(value(row, "resource_tags"), line),
        "x_ServiceCode": product,
        "x_UsageType": usage_type,
        "x_Operation": value(row, "line_item_operation"),
    })
    sp, ri = value(row, "savings_plan_savings_plan_a_r_n"), value(row, "reservation_reservation_a_r_n")

    def charge(category: str, frequency: str, pricing: str, billed: Decimal, effective: Decimal, list_cost: Decimal, **extra: str) -> Dict[str, str]:
        out = dict(base, ChargeCategory=category, ChargeFrequency=frequency, PricingCategory=pricing)
        out.update(BilledCost=text_of(billed), EffectiveCost=text_of(effective), ListCost=text_of(list_cost))
        if category == "Usage" and extra.get("CommitmentDiscountStatus") != "Unused":
            out.update(ConsumedQuantity=text_of(usage_amount), ConsumedUnit=value(row, "pricing_unit"))
        out.update(extra)
        return out

    def commitment(arn: str, category: str, kind_name: str, status: str, quantity: Optional[Decimal], unit: str) -> Dict[str, str]:
        return {
            "CommitmentDiscountId": arn,
            "CommitmentDiscountCategory": category,
            "CommitmentDiscountType": kind_name,
            "CommitmentDiscountStatus": status,
            "CommitmentDiscountQuantity": text_of(quantity),
            "CommitmentDiscountUnit": unit if quantity is not None else "",
        }

    def unused(arn: str, category: str, kind_name: str, quantity: Decimal, cost: Decimal, unit: str) -> Dict[str, str]:
        return charge("Usage", "Usage-Based", "Committed", ZERO, cost, cost, ResourceId=arn, **commitment(arn, category, kind_name, "Unused", quantity, unit))

    if kind == "Usage":
        pricing = "Dynamic" if "SpotUsage" in usage_type else "Standard"
        return [charge("Usage", "Usage-Based", pricing, unblended, unblended, public)], ""
    if kind == "SavingsPlanCoveredUsage":
        effective = amount("savings_plan_savings_plan_effective_cost")
        return [charge("Usage", "Usage-Based", "Committed", ZERO, effective, public, **commitment(sp, "Spend", "Savings Plan", "Used", effective, "USD"))], ""
    if kind == "SavingsPlanNegation":
        return [], ""
    if kind in ("SavingsPlanRecurringFee", "SavingsPlanUpfrontFee"):
        total = amount("savings_plan_total_commitment_to_date", unblended)
        frequency = "Recurring" if kind == "SavingsPlanRecurringFee" else "One-Time"
        rows = [charge("Purchase", frequency, "Standard", unblended, ZERO, unblended, **commitment(sp, "Spend", "Savings Plan", "", total if frequency == "Recurring" else None, "USD"))]
        idle = total - amount("savings_plan_used_commitment", total)
        if frequency == "Recurring" and idle > 0:
            rows.append(unused(sp, "Spend", "Savings Plan", idle, idle, "USD"))
        return rows, ""
    if kind == "DiscountedUsage":
        effective = amount("reservation_effective_cost")
        return [charge("Usage", "Usage-Based", "Committed", unblended, effective, public, **commitment(ri, "Usage", "Reserved Instance", "Used", usage_amount, "Hours"))], ""
    if kind == "RIFee":
        rows = [charge("Purchase", "Recurring", "Standard", unblended, ZERO, unblended, **commitment(ri, "Usage", "Reserved Instance", "", usage_amount, "Hours"))]
        idle_hours = amount("reservation_unused_quantity", ZERO)
        idle_cost = amount("reservation_unused_recurring_fee", ZERO) + amount("reservation_unused_amortized_upfront_fee_for_billing_period", ZERO)
        if idle_hours > 0 or idle_cost > 0:
            rows.append(unused(ri, "Usage", "Reserved Instance", idle_hours, idle_cost, "Hours"))
        return rows, ""
    if kind == "Fee" and ri:
        return [charge("Purchase", "One-Time", "Standard", unblended, ZERO, unblended, **commitment(ri, "Usage", "Reserved Instance", "", None, "Hours"))], ""
    if kind == "Fee":
        return [charge("Purchase", "One-Time", "Standard", unblended, unblended, unblended)], ""
    if kind == "Tax":
        return [charge("Tax", "One-Time", "", unblended, unblended, unblended, ResourceId="")], ""
    if kind == "Credit":
        return [charge("Credit", "One-Time", "", unblended, unblended, unblended)], ""
    return [charge("Adjustment", "One-Time", "", unblended, unblended, unblended)], kind or "(empty line item type)"


def map_cur(path: str) -> Tuple[List[Dict[str, str]], Decimal, Dict[str, List[Decimal]]]:
    columns, rows = read_csv(path)
    missing = [c for c in CUR_REQUIRED if c not in columns]
    if missing:
        raise InputError(f"{path}: missing CUR 2.0 column(s) {', '.join(missing)}")
    out: List[Dict[str, str]] = []
    unblended = ZERO
    adjustments: Dict[str, List[Decimal]] = {}
    for number, row in enumerate(rows, start=2):
        mapped, adjusted = map_cur_row(row, number)
        cost = decimal_of(value(row, "line_item_unblended_cost"), "line_item_unblended_cost", number)
        unblended += cost
        if adjusted:
            entry = adjustments.setdefault(adjusted, [ZERO, ZERO])
            entry[0] += 1
            entry[1] += cost
        out.extend(mapped)
    return out, unblended, adjustments


def write_focus(rows: Iterable[Dict[str, str]], handle: Any) -> None:
    writer = csv.DictWriter(handle, fieldnames=list(FOCUS_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def load_resources(doc: Any) -> List[Tuple[str, Dict[str, str]]]:
    mapping = doc.get("ResourceTagMappingList") if isinstance(doc, dict) else None
    if not isinstance(mapping, list):
        raise InputError("resources: expected the JSON printed by aws resourcegroupstaggingapi get-resources (ResourceTagMappingList)")
    out = []
    for item in mapping:
        if not isinstance(item, dict) or not item.get("ResourceARN"):
            raise InputError("resources: every entry needs a ResourceARN")
        out.append((str(item["ResourceARN"]), {str(t.get("Key")): t.get("Value") for t in item.get("Tags") or []}))
    return out


def matches_resource(arn: str, resource_id: str) -> bool:
    return bool(resource_id) and (arn == resource_id or arn.endswith("/" + resource_id) or arn.endswith(":" + resource_id))


def tag_audit(resources: Sequence[Tuple[str, Dict[str, str]]], key: str = "team", policy: Optional[TagPolicy] = None, charges: Sequence[Charge] = ()) -> List[Dict[str, Any]]:
    if policy is not None:
        key = policy.key
    costs: Dict[str, Decimal] = {}
    for charge in charges:
        if charge.resource:
            costs[charge.resource] = costs.get(charge.resource, ZERO) + charge.effective
    audit = []
    for arn, tags in resources:
        found, reason = tag_value(tags, key)
        if found is None:
            status = reason or f"no {key} tag"
        elif policy is not None and not policy.allows(found):
            status = f"value {found!r} not allowed by the tag policy"
        else:
            status = "ok"
        cost = sum((amount for resource, amount in costs.items() if matches_resource(arn, resource)), ZERO)
        audit.append({"resource": arn, "value": found, "status": status, "ok": status == "ok", "effective": cost})
    return sorted(audit, key=lambda a: (a["ok"], -a["effective"], a["resource"]))


def cost_explorer_totals(doc: Any, key: str, metric: str) -> Dict[str, Decimal]:
    periods = doc.get("ResultsByTime") if isinstance(doc, dict) else None
    if not isinstance(periods, list):
        raise InputError("cost explorer: expected the JSON printed by aws ce get-cost-and-usage (ResultsByTime)")
    prefix = key + "$"
    totals: Dict[str, Decimal] = {}
    for period in periods:
        for group in period.get("Groups") or []:
            label = (group.get("Keys") or [""])[0]
            if not label.startswith(prefix):
                raise InputError(f"cost explorer: group {label!r} is not grouped by the tag {key} (run it with --group-by Type=TAG,Key={key})")
            try:
                amount = Decimal(str(group["Metrics"][metric]["Amount"]))
            except (KeyError, TypeError, InvalidOperation):
                raise InputError(f"cost explorer: group {label!r} has no {metric} amount (request it with --metrics {metric})")
            name = label[len(prefix):]
            totals[name] = totals.get(name, ZERO) + amount
    return totals


def ce_diff(charges: Sequence[Charge], doc: Any, key: str = "team", metric: str = "AmortizedCost") -> List[Dict[str, Any]]:
    column = CE_METRICS[metric]
    ours: Dict[str, Decimal] = {}
    for charge in charges:
        found, _ = tag_value(charge.tags, key)
        name = found if found is not None else ""
        ours[name] = ours.get(name, ZERO) + (charge.effective if column == "effective" else charge.billed)
    theirs = cost_explorer_totals(doc, key, metric)
    rows = []
    for name in sorted(set(ours) | set(theirs)):
        mine = ours.get(name, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        other = theirs.get(name, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        rows.append({"group": f"{key}${name}", "billjoin": mine, "cost_explorer": other, "difference": mine - other, "ok": mine == other})
    return rows


def as_json(item: Any) -> Any:
    if isinstance(item, Decimal):
        return format(item.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize(), "f") if item != item.quantize(CENT) else money(item)
    if isinstance(item, dict):
        return {k: as_json(v) for k, v in item.items()}
    if isinstance(item, (list, tuple)):
        return [as_json(v) for v in item]
    return item


def share(amount: Decimal, total: Decimal) -> str:
    return money(HUNDRED * amount / total) if total else "0.00"


def report_json(report: Report) -> Dict[str, Any]:
    a = report.allocation
    return as_json({
        "rows": report.rows,
        "currency": report.currency,
        "providers": report.providers,
        "period": {"first_day": report.first_day, "last_day": report.last_day},
        "totals": {"billed": money(report.billed), "effective": money(report.effective), "on_demand_equivalent": money(report.list_usage)},
        "findings": [{"line": f.line, "column": f.column, "rule": f.rule} for f in report.findings],
        "allocation": {
            "tag": a.key,
            "spread_shared": a.spread,
            "lines": [{"name": n, "effective": money(a.lines[n]), "direct": money(a.direct.get(n, ZERO)), "share": share(a.lines[n], a.total)} for n in a.ordered()],
            "sum": money(sum(a.lines.values(), ZERO)),
            "total": money(a.total),
            "reconciles": a.reconciles,
            "untagged": [{"source": u["source"], "reason": u["reason"], "effective": money(u["effective"])} for u in a.untagged],
        },
        "commitments": [
            {
                "id": s.id,
                "type": s.type,
                "category": s.category,
                "unit": s.unit,
                "utilisation": None if s.utilisation is None else money(s.utilisation),
                "used_quantity": quantity_text(s.used_quantity),
                "unused_quantity": quantity_text(s.unused_quantity),
                "used_effective": money(s.used_effective),
                "unused_effective": money(s.unused_effective),
                "covered_list": money(s.covered_list),
                "savings": money(s.savings),
                "net_savings": money(s.net_savings),
                "purchases_billed": money(s.purchases_billed),
                "observed_hours": quantity_text(s.observed_hours),
                "expected_effective": money(s.expected_effective),
                "amortisation_gap": money(s.amortisation_gap),
            }
            for s in report.commitments
        ],
        "coverage": [
            {"service_category": c.category, "on_demand_equivalent": money(c.list_cost), "covered": money(c.covered_list), "coverage": money(c.coverage), "effective": money(c.effective), "savings": money(c.savings), "esr": money(c.esr)}
            for c in report.coverage
        ],
        "unit_costs": [
            {"line": u["line"], "unit": u["unit"], "quantity": quantity_text(u["quantity"]), "cost": None if u["cost"] is None else money(u["cost"]), "unit_cost": None if u["unit_cost"] is None else quantity_text(u["unit_cost"])}
            for u in report.unit_costs
        ],
        "invoices": [dict(i, billed=money(i["billed"]), invoice_total=money(i["invoice_total"]), difference=money(i["difference"]), tolerance=money(i["tolerance"])) for i in report.invoices],
        "ok": report.ok,
    })


def amortisation_note(s: CommitmentStats) -> str:
    gap = s.amortisation_gap.quantize(CENT, rounding=ROUND_HALF_UP)
    head = f"purchases in this file amortise to {money(s.expected_effective)} over the {quantity_text(s.observed_hours)} h the rows cover; the rows carry {money(s.used_effective + s.unused_effective)}"
    if gap == 0:
        return head + ": they match"
    if gap > 0:
        return head + f": {money(gap)} was paid outside this file (an upfront fee from an earlier period)"
    return head + f": {money(-gap)} is missing (Unused rows or covered usage left out of the export)"


def render_report(report: Report) -> str:
    a = report.allocation
    out = [f"billjoin: {report.rows} rows in {report.currency}, charge periods {report.first_day} to {report.last_day} (end exclusive)" + (f", provider {', '.join(report.providers)}" if report.providers else "")]
    out.append(f"totals: BilledCost {money(report.billed)}  EffectiveCost {money(report.effective)}  on-demand equivalent of usage {money(report.list_usage)}")
    out.append(f"rule checks: {len(report.findings)} finding(s)")
    for f in report.findings[:20]:
        out.append(f"  line {f.line}: {f.column} {f.rule}")
    if len(report.findings) > 20:
        out.append(f"  ... {len(report.findings) - 20} more (--json lists them all)")
    out.append("")
    how = ", unused and shared spread by direct cost" if a.spread else ""
    out.append(f"showback by tag {a.key} (EffectiveCost{how})")
    width = max([len(n) for n in a.lines] + [5])
    for name in a.ordered():
        out.append(f"  {name:<{width}} {money(a.lines[name]):>12} {share(a.lines[name], a.total):>7}%")
    verdict = "reconciles with" if a.reconciles else "DOES NOT reconcile with"
    out.append(f"  {'total':<{width}} {money(sum(a.lines.values(), ZERO)):>12}  {verdict} the EffectiveCost total {money(a.total)}")
    if a.untagged:
        out.append(f"untagged sources ({len(a.untagged)}), largest first")
        for u in a.untagged[:10]:
            out.append(f"  {money(u['effective']):>10}  {u['source']}  ({u['reason']})")
    if report.commitments:
        out.append("")
        out.append("commitments (utilisation = used / (used + unused) in CommitmentDiscountUnit)")
        for s in report.commitments:
            rate = "n/a" if s.utilisation is None else money(s.utilisation) + "%"
            out.append(f"  {s.id}  {s.type or s.category}")
            out.append(f"    utilisation {rate}  used {quantity_text(s.used_quantity)} {s.unit}  unused {quantity_text(s.unused_quantity)} {s.unit}  waste {money(s.unused_effective)}")
            out.append(f"    savings vs on-demand {money(s.savings)}, net of waste {money(s.net_savings)}")
            out.append(f"    amortised: {amortisation_note(s)}")
    if report.coverage:
        out.append("")
        out.append("coverage by ServiceCategory (on-demand equivalent = ListCost of usage; ESR = savings / on-demand equivalent)")
        cwidth = max(len(c.category) for c in report.coverage)
        for c in report.coverage:
            out.append(f"  {c.category:<{cwidth}}  on-demand {money(c.list_cost):>10}  covered {money(c.coverage):>6}%  effective {money(c.effective):>10}  ESR {money(c.esr):>6}%")
    if report.unit_costs:
        out.append("")
        out.append("unit costs")
        for u in report.unit_costs:
            if u["cost"] is None:
                out.append(f"  {u['line']}: no allocation line with this name")
            else:
                out.append(f"  {u['line']}: {money(u['cost'])} / {quantity_text(u['quantity'])} {u['unit']} = {quantity_text(u['unit_cost'])} per {u['unit']}")
    for i in report.invoices:
        state = "ok" if i["ok"] else "MISMATCH"
        out.append(f"invoice {i['invoice']}: {i['rows']} rows, BilledCost {money(i['billed'])}, invoice {money(i['invoice_total'])}, difference {money(i['difference'])}, tolerance {money(i['tolerance'])}: {state}")
    return "\n".join(out)


def render_anomalies(days: Sequence[str], found: Sequence[Anomaly], by: str, window: int, threshold_pct: Decimal, min_impact: Decimal) -> str:
    span = f"{days[0]} to {days[-1]}" if days else "no day (fewer days than the window)"
    out = [f"anomalies by {by}: rolling median of the previous {window} days, flag when the increase is at least {money(min_impact)} and {threshold_pct}%; evaluated {span}"]
    if not found:
        out.append("  none")
    for a in found:
        percent = "new spend" if a.percent is None else f"+{money(a.percent)}%"
        out.append(f"  {a.day}  {a.group}  actual {money(a.actual)}  baseline {money(a.baseline)}  +{money(a.impact)} ({percent})")
        for source, delta in a.contributors:
            out.append(f"      +{money(delta)}  {source}")
    return "\n".join(out)


def anomalies_json(days: Sequence[str], found: Sequence[Anomaly], by: str, window: int) -> Dict[str, Any]:
    return as_json({
        "by": by,
        "window": window,
        "evaluated": list(days),
        "anomalies": [
            {
                "group": a.group,
                "day": a.day,
                "actual": money(a.actual),
                "baseline": money(a.baseline),
                "impact": money(a.impact),
                "percent": None if a.percent is None else money(a.percent),
                "contributors": [{"source": s, "delta": money(d)} for s, d in a.contributors],
            }
            for a in found
        ],
    })


def policy_from(path: Optional[str], key: str) -> Optional[TagPolicy]:
    return load_tag_policy(read_json(path), key) if path else None


def cmd_report(args: argparse.Namespace) -> int:
    charges = load_focus(args.focus)
    report = build_report(
        charges,
        key=args.tag,
        policy=policy_from(args.tag_policy, args.tag),
        spread_shared=args.spread_shared,
        units=read_json(args.units) if args.units else None,
        invoices=args.invoice or [],
    )
    print(json.dumps(report_json(report), indent=2) if args.json else render_report(report))
    return 0 if report.ok else 1


def cmd_anomalies(args: argparse.Namespace) -> int:
    charges = load_focus(args.focus)
    single_currency(charges)
    days, found = find_anomalies(charges, args.by, args.tag, policy_from(args.tag_policy, args.tag), args.window, args.threshold_pct, args.min_impact)
    print(json.dumps(anomalies_json(days, found, args.by, args.window), indent=2) if args.json else render_anomalies(days, found, args.by, args.window, args.threshold_pct, args.min_impact))
    return 1 if found else 0


def cmd_map_cur(args: argparse.Namespace) -> int:
    rows, unblended, adjustments = map_cur(args.cur)
    billed = sum((Decimal(r["BilledCost"]) for r in rows), ZERO)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8", newline="") as handle:
                write_focus(rows, handle)
        except OSError as exc:
            raise InputError(f"cannot write {args.output}: {exc.strerror}")
    else:
        write_focus(rows, sys.stdout)
    for kind, (count, cost) in sorted(adjustments.items()):
        print(f"map-cur: booked {count} {kind} line(s) totalling {money(cost)} as Adjustment", file=sys.stderr)
    same = money(billed) == money(unblended)
    print(f"map-cur: FOCUS rows {len(rows)}; sum of BilledCost {money(billed)} {'=' if same else '!='} sum of line_item_unblended_cost {money(unblended)}", file=sys.stderr)
    return 0 if same else 1


def cmd_tags(args: argparse.Namespace) -> int:
    resources = load_resources(read_json(args.resources))
    charges = load_focus(args.bill) if args.bill else []
    audit = tag_audit(resources, args.tag, policy_from(args.tag_policy, args.tag), charges)
    if args.json:
        print(json.dumps(as_json(audit), indent=2))
    else:
        bad = [a for a in audit if not a["ok"]]
        print(f"tags: {len(resources)} resource(s), {len(bad)} without a compliant {args.tag} tag")
        for a in audit:
            cost = f"{money(a['effective']):>10}  " if args.bill else ""
            print(f"  {'ok ' if a['ok'] else 'FIX'}  {cost}{a['resource']}  {a['status'] if not a['ok'] else a['value']}")
    return 0 if all(a["ok"] for a in audit) else 1


def cmd_ce_diff(args: argparse.Namespace) -> int:
    charges = load_focus(args.focus)
    single_currency(charges)
    rows = ce_diff(charges, read_json(args.cost_explorer), args.tag, args.metric)
    if args.json:
        print(json.dumps(as_json(rows), indent=2))
    else:
        column = CE_METRICS[args.metric]
        print(f"ce-diff: Cost Explorer {args.metric} against billjoin {'EffectiveCost' if column == 'effective' else 'BilledCost'}, grouped by tag {args.tag}")
        for r in rows:
            print(f"  {r['group']:<24} billjoin {money(r['billjoin']):>10}  cost explorer {money(r['cost_explorer']):>10}  {'ok' if r['ok'] else 'DIFF ' + money(r['difference'])}")
    return 0 if all(r["ok"] for r in rows) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="billjoin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="validate, allocate, amortise and print the showback report")
    report.add_argument("focus", help="FOCUS cost and usage CSV (- reads stdin)")
    report.add_argument("--tag", default="team", help="tag key that names the owner (default team)")
    report.add_argument("--tag-policy", help="AWS Organizations tag policy JSON; values it does not allow count as untagged")
    report.add_argument("--spread-shared", action="store_true", help="spread unused commitments and shared charges over the teams by direct cost")
    report.add_argument("--units", help='JSON {"units": {"LINE": {"unit": "1k orders", "quantity": 840}}} for unit costs')
    report.add_argument("--invoice", action="append", type=parse_invoice, metavar="ID=AMOUNT", help="check the BilledCost of an InvoiceId against the invoice total")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.set_defaults(run=cmd_report)

    anomalies = sub.add_parser("anomalies", help="flag days whose usage cost jumps above a rolling median")
    anomalies.add_argument("focus")
    anomalies.add_argument("--by", choices=GROUPINGS, default="tag", help="group by the owner tag, ServiceName or SubAccountId")
    anomalies.add_argument("--tag", default="team")
    anomalies.add_argument("--tag-policy")
    anomalies.add_argument("--window", type=int, default=7, help="days of history in the baseline (default 7)")
    anomalies.add_argument("--threshold-pct", type=Decimal, default=Decimal("25"), help="minimum increase over the baseline, in percent (default 25)")
    anomalies.add_argument("--min-impact", type=Decimal, default=Decimal("20"), help="minimum increase in currency units (default 20)")
    anomalies.add_argument("--json", action="store_true")
    anomalies.set_defaults(run=cmd_anomalies)

    mapper = sub.add_parser("map-cur", help="map a CUR 2.0 style CSV onto FOCUS columns")
    mapper.add_argument("cur", help="CUR 2.0 CSV with line_item_*, pricing_*, savings_plan_*, reservation_* and resource_tags columns")
    mapper.add_argument("-o", "--output", help="write the FOCUS CSV here instead of stdout")
    mapper.set_defaults(run=cmd_map_cur)

    tags = sub.add_parser("tags", help="audit aws resourcegroupstaggingapi get-resources output against the owner tag")
    tags.add_argument("--resources", required=True, help="JSON from aws resourcegroupstaggingapi get-resources")
    tags.add_argument("--tag", default="team")
    tags.add_argument("--tag-policy")
    tags.add_argument("--bill", help="FOCUS CSV, to rank the resources that need fixing by EffectiveCost")
    tags.add_argument("--json", action="store_true")
    tags.set_defaults(run=cmd_tags)

    diff = sub.add_parser("ce-diff", help="compare aws ce get-cost-and-usage grouped by a tag with the FOCUS rows")
    diff.add_argument("focus")
    diff.add_argument("--cost-explorer", required=True, help="JSON from aws ce get-cost-and-usage --group-by Type=TAG,Key=KEY")
    diff.add_argument("--tag", default="team")
    diff.add_argument("--metric", choices=sorted(CE_METRICS), default="AmortizedCost")
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(run=cmd_ce_diff)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.run(args)
    except InputError as exc:
        print(f"billjoin: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
