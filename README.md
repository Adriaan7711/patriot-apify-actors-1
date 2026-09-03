# SEC Structured Filings — Allocator & Sponsor Mapper

Built for the Patriot Holdings Fund VI raise. Pulls two public, structured SEC
sources and emits one flat, Clay-ready dataset.

| Source | What it gives you | Persona it feeds |
|---|---|---|
| **Form D** (EDGAR) | Sponsors raising private offerings, their related persons (officers, directors, promoters), the placement agent / managing broker-dealer, offering size, amount already sold, minimum check | Sponsor / co-GP map, and the BD channel |
| **Form ADV** (IARD monthly bulk report) | Registered investment advisers and exempt reporting advisers: legal name, CRD, address, website, CCO name + email, RAUM, client types, private-fund count | Principal Allocator (RIA / MFO), Portfolio Constructor (FoF) |

## One thing to know before you start

**Form ADV is not on EDGAR.** Advisers file it through IARD, and it surfaces on
adviserinfo.sec.gov. The SEC republishes the whole population monthly as a ZIP of
spreadsheets, and that bulk file is what this Actor uses — it is cleaner, faster
and less legally fussy than scraping the IAPD site. EDGAR carries only ADV-E and
ADV-NR, which are not what you want.

Form D *is* on EDGAR, in structured XML, and this Actor reads it three ways.

## Setup

1. Apify Console → **Actors → Develop → New → Empty Python project**.
2. Replace the files with the ones in this folder, keeping the paths:

```
.actor/actor.json
.actor/Dockerfile
.actor/input_schema.json
.actor/dataset_schema.json
requirements.txt
src/__init__.py
src/__main__.py
src/main.py
src/sec.py
src/form_d.py
src/form_adv.py
src/scoring.py
```

3. **Build**.
4. Set `secUserAgent` before the first run — e.g. `Patriot Holdings jer@patriotholdings.com`.
   The SEC requires a descriptive User-Agent with a live contact email on every
   automated request and will block you without one. The Actor refuses to start
   if it is missing.

Memory: 1024 MB is plenty for Form D. Give it **4096 MB** if you are pulling the
registered-adviser ADV feed, which is a large spreadsheet.

No proxy needed. SEC data is public and the Actor stays under 8 requests/second
against the SEC's stated 10/second ceiling.

## The three Form D modes

- **`daily_index`** — sweeps every Form D and D/A filed in the lookback window
  from EDGAR's daily index, then pulls each filing's `primary_doc.xml`. Complete
  coverage, filtered structurally. **This is the one to schedule.**
- **`full_text`** — EDGAR full-text search for thesis phrases
  (`"industrial outdoor storage"`, `"manufactured housing"`, …). Cheap, targeted,
  and good for finding sponsors whose *name* gives nothing away. Note that
  full-text search only reaches back to 2001 and caps at 10,000 hits per query.
- **`quarterly`** — SEC's own Form D bulk data sets (quarterly TSV zips, 2009 →
  present). Run this once per quarter you want to backfill, then never again.

Suggested first pass: `quarterly` over the last 4 quarters to build the base,
then a weekly scheduled `daily_index` run with `lookbackDays: 10` to catch new
filings.

## What the mandate filters actually do

Everything Patriot-specific lives in `src/scoring.py`, so you can tune the
thesis without touching crawler code.

- **Sector-fit score** — positive weights on manufactured housing, mobile home,
  MHC/MHP, self storage, industrial, small-bay, flex, industrial outdoor
  storage, IOS, affordable/workforce housing, net lease, 1031, DST. Negative
  weights on multifamily, apartment, student and senior housing, farmland,
  timber, oil and gas, crypto, venture, biotech, cannabis, hotel. A record is
  scored on its current name, industry group, fund type, client types and the
  sales-compensation note — deliberately **not** on former names, so an issuer
  that used to be "Caprock Oil" isn't punished for it.
- **Hard exclusions** — pensions and retirement systems, sovereign wealth,
  the mega-institutional names ($5B+ shops that won't look at this fund),
  the named do-not-contact, and issuer names that read as operating businesses
  rather than capital sources.
- **Structural filters** — Form D industry group, offering size band, state,
  pooled-fund-only. Filings that report an *indefinite* offering (Form D's
  $100B sentinel) are never dropped by the size ceiling.
- **Dedupe** — within the run, and against `excludeFirmNames`. Paste your
  existing investor database into that field; matching is on a normalized name,
  so "Acme Capital, LLC" and "ACME CAPITAL" collapse to one.

**Watch this one:** raising `minSectorFitScore` above 0 will gut the Form ADV
side, because adviser names rarely contain sector language and therefore score
0. Screen the ADV side with `advMinRaum` / `advMaxRaum` / `advStates` instead.
The Actor logs a warning if you set both.

Run `keepExcludedForReview: true` the first time. It emits everything with
`excluded_reason` and `sector_fit_reason` populated so you can see exactly what
the filters are throwing away before you trust them.

## Output

One flat row per firm and per person (`outputMode` controls which). Key fields:

`record_type`, `source`, `persona_hint`, `sector_fit_score`, `sector_fit_reason`,
`excluded_reason`, `dedupe_key`, `firm_name`, `firm_cik`, `firm_crd`,
`firm_website`, `address_*`, `phone`, `person_full_name`, `person_first_name`,
`person_last_name`, `person_title`, `person_relationship`, `person_email`,
`industry_group`, `fund_type`, `is_pooled_fund`, `total_offering_amount`,
`total_amount_sold`, `minimum_investment`, `investor_count`,
`broker_dealer_name`, `placement_agent`, `raum_usd`, `num_clients`,
`client_types`, `private_fund_count`, `filing_type`, `filing_date`,
`accession_number`, `filing_url`, `scraped_at`.

Two key-value store records are written every run:
- `RUN_SUMMARY` — counts of what was seen, kept and dropped, and why.
- `ADV_COLUMNS_registered` / `ADV_COLUMNS_exempt` — every column header found in
  the SEC spreadsheet plus the mapping the Actor inferred. **If the ADV side
  ever comes back empty, read this record first** — the SEC renames those
  columns occasionally, and the fix is one line in `COLUMN_PATTERNS`.

## Legal footing

Form D and the ADV bulk reports are public SEC disclosures published for exactly
this kind of use, and this Actor reads them at a self-imposed rate under the
SEC's published limit with an identifying User-Agent. That is the clean part.

What comes *after* is where the care is needed: this dataset is a research and
targeting input, not a distribution list. Under 506(c) the general-solicitation
question is about how you communicate, not how you built the list, and every
outreach message that comes out of this pipeline should still go through the
compliance review gate before it's sent. Accredited-investor verification
obligations are unchanged.
