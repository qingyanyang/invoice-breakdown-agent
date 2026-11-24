# Invoice Breakdown specifications and fixtures

The business owner confirmed **Invoice.Breakdown.Codex.md**, version **2026-10-05**. Its SHA-256 is:

```text
28a43a6165cbdc62820251fdc969234da8cbd31e0d147151f0f1960f550a98a4
```

Obtain the approved Markdown file from the business owner. Verify it with
`shasum -a 256 /path/to/Invoice.Breakdown.Codex.md`. Read the approved file in
full when first loaded for a task and pin one version for that task.

If the reference is missing or copies conflict, record the unresolved input in
the work item. Include the expected filename and version, where you checked,
any candidate versions and checksums you found, and the rule decisions or
fixture expectations that depend on it. Ask the business owner for the
authorized path, attachment, or version. Keep the affected decisions and
expectations pending until the owner confirms the copy and you verify its
version and checksum.

| Workflow concern | Approved specification sections |
| --- | --- |
| Authorization, current-task inputs and isolation | I, XI |
| Source retention, reconciliation, hierarchy and markup | II, VIII.3 |
| Option, Removal and Deletion | III |
| Trade classification and protected Access Panel additions | IV.1–IV.7 |
| Areas and additional items | V |
| GST basis and decimal amounts | VI |
| Protected amounts, allocation limits and rounding | VII.1–VII.5 |
| Output sheets, presentation and QA | VIII–X |
| Delivery summary | XII |

## Synthetic workbooks

Use the source workbook and the current request as workflow inputs. Keep these expectations in the verification harness. The workbooks, names and figures are invented. The reconstructed MetLife workbook may be used privately for exploration. Keep the real sample pack outside the repository and its unverified comparison workbook outside fresh-generation inputs. Apply the approved reference above when the sample pack includes draft guidance.

### Reconciliation and protected allocation

`reconciliation.xlsx` has two source sheets, **Trade Summary** and **Notes**.

| Scenario | Evidence and expected result | Specification |
| --- | --- | --- |
| Create without a target | `Trade Summary!D12` is 260.00. Use the two marked-up child amounts 110.00 and 55.00 in place of their 165.00 subtotal. Doors is 60.00 − 10.00 = 50.00. Make Good is 25.00 and Access Panel is 20.00. Preserve both sheets. | II, IV.1–IV.6, VIII.3 |
| Create with target 282.54 excluding GST and an expressly authorized Feature Access Panel addition of 1.01 outside the source total | Included protected amounts are 45.00. Final protected amounts are 46.01. Eligible source is 215.00 and eligible target is 236.53. Apply the factor 236.53 ÷ 215.00. The rounded eligible amounts are 121.02, 60.51 and 55.01 before residual allocation. Apply −0.01 to Partition, the largest eligible amount. Final Partition is 121.01, Glazing is 60.51, Doors is 55.01, Make Good is 25.00 and Access Panel is 21.01. Total is 282.54 with 0.00 difference. | VI, VII.1–VII.5 |
| Create with target 282.54 and unspecified GST basis | Ask for the target GST basis before final allocation. | VI |

### Missing formula cache

`missing-formula-cache.xlsx` contains the same source data as `reconciliation.xlsx`
with the saved result for `Trade Summary!D12` removed. Expect the total formula and
its source coordinate to be retained. Treat the result as unverified until an actual
recalculation or equivalent check establishes it. The independently calculated
260.00 is a check value. See VIII.3 and X.

### Ambiguous scope

`scope-ambiguity.xlsx` has one source sheet. `Trade Summary!D7` is 75.00. The base row is also 75.00, and the +15.00 Option and −15.00 Deletion cancel. The total alone cannot establish which scope the source includes. Expect a clarification before final allocation. `A4` identifies combined levels without independent amounts. Preserve the combined attribution. See III and V.

## Verification

Run `python3 tests/fixtures/invoice_breakdown/verify.py` from the repository root. It checks the packaged source coordinates, formula text and numeric caches, including the intentionally missing cache. It independently checks the case arithmetic with Decimal. The fixture verification covers source data and expectations. Workbook generation, recalculation in customer Excel, and production delivery QA belong to later issues.
