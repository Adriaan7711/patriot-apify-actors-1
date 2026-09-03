"""Form D pipeline.

Three ways in, all of them public and structured:

  daily_index  - sweep every Form D / D-A filed in a date window from EDGAR's
                 daily index, then pull each filing's primary_doc.xml.
                 Complete coverage, filtered structurally. Use for the standing
                 scheduled run.
  full_text    - EDGAR full-text search (efts.sec.gov) for thesis keywords.
                 Cheap and targeted. Use for keyword sweeps like
                 "industrial outdoor storage".
  quarterly    - SEC DERA's Form D structured data sets (quarterly TSV zips).
                 Use once, to backfill history.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from apify import Actor

from .sec import SecClient

ARCHIVES = 'https://www.sec.gov/Archives'
FULL_TEXT_SEARCH = 'https://efts.sec.gov/LATEST/search-index'
FORM_D_DATASET_URLS = (
    'https://www.sec.gov/files/structureddata/data/form-d-data-sets/{quarter}_d.zip',
    'https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/{quarter}_d.zip',
)

_ACCESSION_RE = re.compile(r'(\d{10}-\d{2}-\d{6})')
_FILENAME_RE = re.compile(r'(edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt)')
_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


# --- small XML helpers ---------------------------------------------------

def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ''
    found = node.find(path)
    return (found.text or '').strip() if found is not None and found.text else ''


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).lower() == 'true'


def _money(node: ET.Element | None, path: str) -> int | None:
    raw = _text(node, path)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def filing_url(cik: str, accession: str) -> str:
    return f'{ARCHIVES}/edgar/data/{int(cik)}/{accession.replace("-", "")}/primary_doc.xml'


def filing_index_url(cik: str, accession: str) -> str:
    return f'{ARCHIVES}/edgar/data/{int(cik)}/{accession.replace("-", "")}/'


# --- discovery: daily index ---------------------------------------------

def _daily_index_urls(start: date, end: date) -> list[str]:
    urls = []
    day = start
    while day <= end:
        if day.weekday() < 5:  # EDGAR only publishes on business days
            quarter = (day.month - 1) // 3 + 1
            urls.append(
                f'{ARCHIVES}/edgar/daily-index/{day.year}/QTR{quarter}/'
                f'form.{day.strftime("%Y%m%d")}.idx'
            )
        day += timedelta(days=1)
    return urls


def parse_form_index(text: str, form_types: set[str]) -> list[dict]:
    """Parse an EDGAR form.*.idx file into {form_type, cik, accession, date}.

    The .idx layout is fixed-width, but column widths have shifted over the
    years, so we anchor on the file path instead of on character offsets.
    """
    # Column widths in form.idx have shifted over the years, so read the
    # Form Type column width off the header row when it is present.
    form_col_width = 12
    for line in text.splitlines():
        if line.lstrip().startswith('Form Type') and 'Company Name' in line:
            form_col_width = line.index('Company Name')
            break

    rows: list[dict] = []
    for line in text.splitlines():
        match = _FILENAME_RE.search(line)
        if not match:
            continue
        form_type = line[:form_col_width].strip().upper()
        if form_type not in form_types:
            continue
        _, cik, accession = match.groups()
        date_match = _DATE_RE.search(line)
        rows.append(
            {
                'form_type': form_type,
                'cik': cik,
                'accession': accession,
                'filing_date': date_match.group(1) if date_match else '',
            }
        )
    return rows


async def discover_daily_index(
    client: SecClient, lookback_days: int, include_amendments: bool
) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=max(lookback_days, 1))
    form_types = {'D', 'D/A'} if include_amendments else {'D'}
    found: list[dict] = []
    for url in _daily_index_urls(start, end):
        text = await client.get_text(url, allow_404=True)
        if text is None:
            continue
        rows = parse_form_index(text, form_types)
        Actor.log.info('daily index %s -> %s Form D filings', url.rsplit('/', 1)[-1], len(rows))
        found.extend(rows)
    return found


# --- discovery: full-text search ----------------------------------------

async def discover_full_text(
    client: SecClient,
    queries: list[str],
    start: str,
    end: str,
    include_amendments: bool,
    max_per_query: int,
) -> list[dict]:
    forms = 'D,D/A' if include_amendments else 'D'
    found: list[dict] = []
    for query in queries:
        offset = 0
        while offset < min(max_per_query, 10_000):
            payload = await client.get_json(
                FULL_TEXT_SEARCH,
                params={
                    'q': f'"{query}"',
                    'forms': forms,
                    'startdt': start,
                    'enddt': end,
                    'from': offset,
                },
            )
            hits = (payload or {}).get('hits', {}).get('hits', [])
            if not hits:
                break
            for hit in hits:
                accession_match = _ACCESSION_RE.search(hit.get('_id', ''))
                ciks = hit.get('_source', {}).get('ciks') or []
                if not accession_match or not ciks:
                    continue
                found.append(
                    {
                        'form_type': (hit.get('_source', {}).get('root_form') or 'D').upper(),
                        'cik': ciks[0],
                        'accession': accession_match.group(1),
                        'filing_date': hit.get('_source', {}).get('file_date', ''),
                        'matched_query': query,
                    }
                )
            Actor.log.info('full-text "%s" offset %s -> %s hits', query, offset, len(hits))
            offset += len(hits)
            if len(hits) < 10:
                break
    return found


# --- parsing: primary_doc.xml -------------------------------------------

def parse_primary_doc(xml_text: str) -> dict:
    """Flatten a Form D primary_doc.xml into issuer + offering + people."""
    root = ET.fromstring(xml_text)
    issuer = root.find('primaryIssuer')
    offering = root.find('offeringData')

    address = issuer.find('issuerAddress') if issuer is not None else None
    previous_names = [
        (n.text or '').strip()
        for n in root.findall('.//edgarPreviousNameList/previousName')
        if n.text
    ]

    fund_type = _text(offering, 'industryGroup/investmentFundInfo/investmentFundType')
    is_pooled = bool(fund_type) or _text(
        offering, 'industryGroup/industryGroupType'
    ) == 'Pooled Investment Fund'

    recipients = []
    for node in root.findall('.//salesCompensationList/recipient'):
        recipients.append(
            {
                'recipient_name': _text(node, 'recipientName'),
                'recipient_crd': _text(node, 'recipientCRDNumber'),
                'broker_dealer_name': _text(node, 'associatedBDName'),
                'broker_dealer_crd': _text(node, 'associatedBDCRDNumber'),
            }
        )

    people = []
    for node in root.findall('.//relatedPersonsList/relatedPersonInfo'):
        first = _text(node, 'relatedPersonName/firstName')
        middle = _text(node, 'relatedPersonName/middleName')
        last = _text(node, 'relatedPersonName/lastName')
        person_address = node.find('relatedPersonAddress')
        relationships = [
            (r.text or '').strip()
            for r in node.findall('relatedPersonRelationshipList/relationship')
            if r.text
        ]
        people.append(
            {
                'person_first_name': first,
                'person_last_name': last,
                'person_full_name': ' '.join(p for p in (first, middle, last) if p),
                'person_title': _text(node, 'relationshipClarification'),
                'person_relationship': ', '.join(relationships),
                'person_city': _text(person_address, 'city'),
                'person_state': _text(person_address, 'stateOrCountry'),
            }
        )

    return {
        'firm_name': _text(issuer, 'entityName'),
        'firm_cik': _text(issuer, 'cik'),
        'entity_type': _text(issuer, 'entityType'),
        'jurisdiction_of_inc': _text(issuer, 'jurisdictionOfInc'),
        'previous_names': '; '.join(previous_names),
        'phone': _text(issuer, 'issuerPhoneNumber'),
        'address_street': ' '.join(
            p for p in (_text(address, 'street1'), _text(address, 'street2')) if p
        ),
        'address_city': _text(address, 'city'),
        'address_state': _text(address, 'stateOrCountry'),
        'address_state_name': _text(address, 'stateOrCountryDescription'),
        'address_zip': _text(address, 'zipCode'),
        'industry_group': _text(offering, 'industryGroup/industryGroupType'),
        'fund_type': fund_type,
        'is_pooled_fund': is_pooled,
        'revenue_range': _text(offering, 'issuerSize/revenueRange'),
        'net_asset_value_range': _text(offering, 'issuerSize/aggregateNetAssetValueRange'),
        'is_amendment': _flag(offering, 'typeOfFiling/newOrAmendment/isAmendment'),
        'date_of_first_sale': _text(offering, 'typeOfFiling/dateOfFirstSale/value'),
        'federal_exemptions': ', '.join(
            (i.text or '').strip()
            for i in root.findall('.//federalExemptionsExclusions/item')
            if i.text
        ),
        'minimum_investment': _money(offering, 'minimumInvestmentAccepted'),
        'total_offering_amount': _money(offering, 'offeringSalesAmounts/totalOfferingAmount'),
        'total_amount_sold': _money(offering, 'offeringSalesAmounts/totalAmountSold'),
        'total_remaining': _money(offering, 'offeringSalesAmounts/totalRemaining'),
        'investor_count': _money(offering, 'investors/totalNumberAlreadyInvested'),
        'has_non_accredited': _flag(offering, 'investors/hasNonAccreditedInvestors'),
        'sales_compensation_note': _text(
            offering, 'salesCommissionsFindersFees/clarificationOfResponse'
        ),
        'signer_name': _text(offering, 'signatureBlock/signature/nameOfSigner'),
        'signer_title': _text(offering, 'signatureBlock/signature/signatureTitle'),
        'signature_date': _text(offering, 'signatureBlock/signature/signatureDate'),
        'recipients': recipients,
        'people': people,
    }


async def fetch_filing(client: SecClient, ref: dict) -> dict | None:
    url = filing_url(ref['cik'], ref['accession'])
    xml_text = await client.get_text(url, allow_404=True)
    if not xml_text:
        Actor.log.warning('no primary_doc.xml for %s', ref['accession'])
        return None
    try:
        parsed = parse_primary_doc(xml_text)
    except ET.ParseError as exc:
        Actor.log.warning('unparseable XML for %s: %s', ref['accession'], exc)
        return None
    parsed.update(
        {
            'filing_type': ref.get('form_type', 'D'),
            'filing_date': ref.get('filing_date', ''),
            'accession_number': ref['accession'],
            'filing_url': filing_index_url(ref['cik'], ref['accession']),
            'matched_query': ref.get('matched_query', ''),
        }
    )
    return parsed


# --- discovery: quarterly bulk data sets --------------------------------

async def load_quarterly_dataset(client: SecClient, quarter: str) -> list[dict]:
    """Download one DERA Form D quarter and merge its TSVs on ACCESSIONNUMBER."""
    blob = None
    for template in FORM_D_DATASET_URLS:
        blob = await client.get_bytes(template.format(quarter=quarter), allow_404=True)
        if blob:
            break
    if not blob:
        Actor.log.warning('Form D data set not found for quarter %s', quarter)
        return []

    tables: dict[str, dict[str, list[dict]]] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith('.tsv'):
                continue
            with archive.open(name) as handle:
                reader = csv.DictReader(
                    io.TextIOWrapper(handle, encoding='utf-8', errors='replace'),
                    delimiter='\t',
                )
                key = name.rsplit('/', 1)[-1].upper().replace('.TSV', '')
                grouped: dict[str, list[dict]] = {}
                for row in reader:
                    accession = (row.get('ACCESSIONNUMBER') or '').strip()
                    if accession:
                        grouped.setdefault(accession, []).append(row)
                tables[key] = grouped
                Actor.log.info('%s: %s rows across %s filings', key, sum(len(v) for v in grouped.values()), len(grouped))

    submissions = tables.get('FORMDSUBMISSION', {})
    issuers = tables.get('ISSUERS', {})
    offerings = tables.get('OFFERING', {})
    persons = tables.get('RELATEDPERSONS', {})
    recipients = tables.get('RECIPIENTS', {})

    merged: list[dict] = []
    for accession in issuers or submissions:
        issuer_row = (issuers.get(accession) or [{}])[0]
        offering_row = (offerings.get(accession) or [{}])[0]
        submission_row = (submissions.get(accession) or [{}])[0]
        merged.append(
            {
                'firm_name': issuer_row.get('ENTITYNAME', ''),
                'firm_cik': issuer_row.get('CIK', ''),
                'entity_type': issuer_row.get('ENTITYTYPE', ''),
                'jurisdiction_of_inc': issuer_row.get('JURISDICTIONOFINC', ''),
                'phone': issuer_row.get('ISSUERPHONENUMBER', ''),
                'address_street': issuer_row.get('STREET1', ''),
                'address_city': issuer_row.get('CITY', ''),
                'address_state': issuer_row.get('STATEORCOUNTRY', ''),
                'address_zip': issuer_row.get('ZIPCODE', ''),
                'industry_group': offering_row.get('INDUSTRYGROUPTYPE', ''),
                'fund_type': offering_row.get('INVESTMENTFUNDTYPE', ''),
                'is_pooled_fund': bool(offering_row.get('INVESTMENTFUNDTYPE')),
                'minimum_investment': offering_row.get('MINIMUMINVESTMENTACCEPTED') or None,
                'total_offering_amount': offering_row.get('TOTALOFFERINGAMOUNT') or None,
                'total_amount_sold': offering_row.get('TOTALAMOUNTSOLD') or None,
                'investor_count': offering_row.get('TOTALNUMBERALREADYINVESTED') or None,
                'filing_type': submission_row.get('SUBMISSIONTYPE', 'D'),
                'filing_date': submission_row.get('FILING_DATE', ''),
                'accession_number': accession,
                'filing_url': (
                    filing_index_url(issuer_row['CIK'], accession)
                    if issuer_row.get('CIK', '').isdigit()
                    else ''
                ),
                'recipients': [
                    {
                        'recipient_name': r.get('RECIPIENTNAME', ''),
                        'recipient_crd': r.get('RECIPIENTCRDNUMBER', ''),
                        'broker_dealer_name': r.get('ASSOCIATEDBDNAME', ''),
                        'broker_dealer_crd': r.get('ASSOCIATEDBDCRDNUMBER', ''),
                    }
                    for r in recipients.get(accession, [])
                ],
                'people': [
                    {
                        'person_first_name': p.get('FIRSTNAME', ''),
                        'person_last_name': p.get('LASTNAME', ''),
                        'person_full_name': ' '.join(
                            v for v in (
                                p.get('FIRSTNAME', ''), p.get('MIDDLENAME', ''), p.get('LASTNAME', '')
                            ) if v
                        ),
                        'person_title': p.get('RELATIONSHIPCLARIFICATION', ''),
                        'person_relationship': p.get('RELATIONSHIP', ''),
                        'person_city': p.get('CITY', ''),
                        'person_state': p.get('STATEORCOUNTRY', ''),
                    }
                    for p in persons.get(accession, [])
                ],
            }
        )
    return merged
