---
name: cost-allocation-review
description: Review a month of cloud cost before showback or chargeback goes out, by checking the FOCUS export is complete and consistent, reconciling BilledCost to the invoice and the showback to the EffectiveCost total to the cent, working the untagged list by cost, and reviewing commitment utilisation, coverage and amortisation
version: 1.0.0
phase: cloud
lesson: cloud/finops-focus
tags: [finops, focus, cost-allocation, showback, chargeback, tagging, savings-plans, reserved-instances, aws, cost-explorer]
---

# Cost allocation review

Use this before a monthly showback or chargeback is sent, when a team disputes its line, when the showback does not add up to the invoice, or when someone proposes to "spread the rest" over the teams. Work from rows, not totals: the FOCUS cost and usage export (or a CUR 2.0 export mapped onto FOCUS) for the billing period, the invoice totals per invoice ID, the tag policy and, if you publish unit costs, the business volumes. Never edit the export to make numbers match; every correction is a rule, a tag fix or a documented adjustment.

## 1. Is the export complete and readable?

```bash
EXPORT=focus-2026-10.csv
head -1 $EXPORT | tr ',' '\n' | grep -nE '^(ChargeCategory|ChargePeriodStart|ChargePeriodEnd|BilledCost|EffectiveCost|ListCost|BillingCurrency|InvoiceId|CommitmentDiscountId|CommitmentDiscountStatus|CommitmentDiscountQuantity|ServiceCategory|ResourceId|Tags)$'
python3 billjoin.py report $EXPORT --json > report.json; echo "exit $?"
focus-validator --data-file $EXPORT --validate-version 1.2      # the provider's FOCUS version, if you have the validator installed
```

| What you see | Meaning | Next |
|---|---|---|
| exit 2, `missing FOCUS column(s)` with the CUR hint | this is a CUR export | `billjoin map-cur CUR.csv -o focus.csv`; it must print `sum of BilledCost X = sum of line_item_unblended_cost X` |
| exit 2, `more than one BillingCurrency` | several billing currencies in one file | split by `BillingCurrency` and review each; never add them |
| rule findings on `EffectiveCost` of Purchase rows | commitment purchases would be counted twice | fix the export query or the mapping before anything else |
| `ChargePeriodStart` range shorter than the billing period | partial export (still running, or filtered) | wait for the final export or re-run it; do not publish a partial month |
| no `Unused` rows although commitments exist | utilisation will read 100% | check the amortisation gap in step 4; a negative gap confirms rows are missing |

## 2. Reconcile to the invoice and to the cent

```bash
python3 billjoin.py report $EXPORT --invoice INV-ID-1=AMOUNT --invoice INV-ID-2=AMOUNT
python3 -c "import json; d=json.load(open('report.json'))['allocation']; print(d['sum'], d['total'], d['reconciles'])"
```

The sum of BilledCost per `InvoiceId` must match each invoice within the FOCUS tolerance (the larger of 100 currency subunits and half a subunit times the square root of the row count). The showback lines must equal the EffectiveCost total exactly; if they do not, a row was dropped or counted twice and nothing else in this review is meaningful until it is fixed. Expect BilledCost and EffectiveCost to differ by commitment purchases made outside the period; explain the difference with step 4 rather than forcing them equal.

## 3. Work the untagged list, largest first

```bash
python3 billjoin.py report $EXPORT --tag-policy tag-policy.json | sed -n '/untagged sources/,/^$/p'
aws resourcegroupstaggingapi get-resources --resource-type-filters ec2:instance > resources.json
python3 billjoin.py tags --resources resources.json --tag-policy tag-policy.json --bill $EXPORT
aws ce get-cost-and-usage --time-period Start=YYYY-MM-01,End=YYYY-MM-01 --granularity MONTHLY --metrics AmortizedCost \
  --filter '{"Tags": {"Key": "team", "MatchOptions": ["ABSENT"]}}' --group-by Type=DIMENSION,Key=SERVICE
```

| Reason on the untagged line | Confirm with | Fix |
|---|---|---|
| `no team tag` on a resource | `aws ec2 describe-instances --instance-ids ID` (or the service's describe call), CloudTrail `RunInstances`/`Create*` for who created it | tag it; if it is not in `get-resources` output it was never tagged, so the creating pipeline lacks the tag |
| `value 'X' not allowed by the tag policy` | the policy's `tag_value` list | correct the value; add `enforced_for` for that resource type so the wrong value is rejected next time |
| `key 'Team' is not 'team'` | `aws ce list-cost-allocation-tags` shows both keys | retag with the canonical key; the tag policy's `tag_key` fixes the spelling |
| no `ResourceId` (data transfer, support, marketplace) | `ServiceName` and `ChargeDescription` on the rows | an allocation rule (by `SubAccountId` or service), not a tag |
| tag present on the resource but missing from the bill | `aws ce list-cost-allocation-tags --status Inactive` | activate the key as a cost allocation tag; it applies from activation, ask for a backfill for earlier months |

Never spread `(untagged)` over the teams to make the sheet look complete. Publish it as a line, with its top resources and owners.

## 4. Commitments: utilisation, coverage, amortisation

```bash
python3 billjoin.py report $EXPORT | sed -n '/^commitments/,/^unit costs/p'
aws ce get-savings-plans-utilization --time-period Start=YYYY-MM-01,End=YYYY-MM-01
aws ce get-savings-plans-coverage --time-period Start=YYYY-MM-01,End=YYYY-MM-01 --granularity MONTHLY
aws ce get-reservation-utilization --time-period Start=YYYY-MM-01,End=YYYY-MM-01
aws ce get-reservation-coverage --time-period Start=YYYY-MM-01,End=YYYY-MM-01
```

| Situation | Verdict |
|---|---|
| utilisation below 100% and coverage at or near 100% | over-bought: the unused rows are waste; exchange or let the commitment expire, do not buy more |
| utilisation at 100% and coverage well below target | under-bought: model a purchase at the steady-state usage, not at the peak |
| amortisation gap positive | part of the commitment was paid before this period (upfront fee); expected, explains Effective minus Billed |
| amortisation gap negative | rows are missing (Unused rows, or covered usage filtered out); fix the export before quoting utilisation |
| `(unused commitment)` large and commitments untagged | tag the commitment with its owner, so its waste lands on the team that bought it |

## 5. Shared costs, unit costs and anomalies

```bash
python3 billjoin.py report $EXPORT --tag-policy tag-policy.json --spread-shared --units units.json
python3 billjoin.py anomalies $EXPORT --tag-policy tag-policy.json --window 7 --threshold-pct 25 --min-impact 100
aws ce get-anomalies --date-interval StartDate=YYYY-MM-01,EndDate=YYYY-MM-28
```

Decide once, in writing, whether tax, credits and unused commitments are shown as their own lines (showback) or spread by direct cost (chargeback), and keep it the same every month. Publish unit costs next to totals so a team whose volume doubled is not accused of doubling its spend. Every anomaly in the period needs an owner and an explanation in the report.

## 6. Report

State: the billing period, export version and row count; each invoice ID with BilledCost, invoice total, difference and tolerance; the showback table (line, EffectiveCost, share) with its sum and the EffectiveCost total, which must be equal; the untagged total with its five largest sources, their reasons and owners; each commitment with utilisation, waste, savings and amortisation gap, and coverage per service category; unit costs per team; anomalies with cause and owner; and every rule finding or mapping adjustment that changed a number. Never report "other" as a line, and never close the review with lines that do not add up to the total.
