"""SEC structured-filings Actor for the Patriot Holdings Fund VI raise.

Pulls two public, structured SEC sources and emits one flat, Clay-ready
dataset:

  Form D  (EDGAR)  - sponsors of private offerings and their related persons.
                     A map of who is already raising into private real-asset
                     deals, plus the placement agents carrying that paper.
  Form ADV (IARD)  - registered investment advisers and exempt reporting
                     advisers: the RIA / multi-family-office layer behind the
                     Principal Allocator and Portfolio Constructor personas.
"""

from __future__ import annotations

from datetime import date, timedelta

from apify import Actor

from . import form_adv, form_d
from . import patriot_mandate as scoring
from .sec import SecClient

DEFAULT_QUERIES = [
    'manufactured housing',
    'mobile home community',
    'self storage',
    'industrial outdoor storage',
    'small bay industrial',
    'flex industrial',
    'Delaware statutory trust',
    '1031 exchange',
]


def _iso(value: date) -> str:
    return value.strftime('%Y-%m-%d')


def _passes_form_d_filters(filing: dict, cfg: dict) -> str | None:
    """Return a rejection reason, or None if the filing should be kept."""
    groups = cfg['industryGroups']
    if groups and filing.get('industry_group') and filing['industry_group'] not in groups:
        return f'industry group "{filing["industry_group"]}" not in scope'

    states = cfg['includeStates']
    if states and (filing.get('address_state') or '').upper() not in states:
        return f'state {filing.get("address_state")} not in scope'

    amount = filing.get('total_offering_amount')
    if amount is not None:
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = None
    if amount is not None:
        if cfg['minOfferingAmount'] and amount < cfg['minOfferingAmount']:
            return f'offering ${amount:,} below floor'
        # "Indefinite" filings use a sentinel of $100B; never exclude those.
        if (
            cfg['maxOfferingAmount']
            and amount > cfg['maxOfferingAmount']
            and amount < 99_000_000_000
        ):
            return f'offering ${amount:,} above ceiling'

    if cfg['requirePooledFund'] and not filing.get('is_pooled_fund'):
        return 'not a pooled investment fund'
    return None


def _build_records(filing: dict, source: str, cfg: dict) -> list[dict]:
    """Turn one filing/adviser into firm and/or person rows."""
    people = filing.get('people') or []
    recipients = filing.get('recipients') or []
    broker = recipients[0] if recipients else {}

    score_text = ' '.join(
        str(filing.get(key) or '')
        for key in (
            # Deliberately NOT previous_names: an issuer that used to be
            # "Caprock Oil" should not be penalised for the old name.
            'firm_name', 'firm_dba', 'industry_group', 'fund_type',
            'client_types', 'sales_compensation_note', 'matched_query',
        )
    )
    score, reason = scoring.score_sector_fit(score_text)
    exclusion = scoring.is_hard_excluded(filing.get('firm_name'), filing.get('firm_dba'))

    base = {
        'source': source,
        'persona_hint': scoring.persona_hint('firm', source, score_text),
        'firm_name': filing.get('firm_name', ''),
        'firm_dba': filing.get('firm_dba', ''),
        'firm_cik': filing.get('firm_cik', ''),
        'firm_crd': filing.get('firm_crd', ''),
        'sec_number': filing.get('sec_number', ''),
        'firm_website': filing.get('firm_website', ''),
        'address_street': filing.get('address_street', ''),
        'address_city': filing.get('address_city', ''),
        'address_state': filing.get('address_state', ''),
        'address_zip': filing.get('address_zip', ''),
        'address_country': filing.get('address_country', 'US'),
        'phone': filing.get('phone', ''),
        'entity_type': filing.get('entity_type', ''),
        'jurisdiction_of_inc': filing.get('jurisdiction_of_inc', ''),
        'previous_names': filing.get('previous_names', ''),
        'industry_group': filing.get('industry_group', ''),
        'fund_type': filing.get('fund_type', ''),
        'is_pooled_fund': bool(filing.get('is_pooled_fund')),
        'filing_type': filing.get('filing_type', ''),
        'filing_date': filing.get('filing_date', ''),
        'accession_number': filing.get('accession_number', ''),
        'filing_url': filing.get('filing_url', ''),
        'date_of_first_sale': filing.get('date_of_first_sale', ''),
        'federal_exemptions': filing.get('federal_exemptions', ''),
        'minimum_investment': filing.get('minimum_investment'),
        'total_offering_amount': filing.get('total_offering_amount'),
        'total_amount_sold': filing.get('total_amount_sold'),
        'investor_count': filing.get('investor_count'),
        'broker_dealer_name': broker.get('broker_dealer_name', ''),
        'broker_dealer_crd': broker.get('broker_dealer_crd', ''),
        'placement_agent': broker.get('recipient_name', ''),
        'raum_usd': filing.get('raum_usd'),
        'discretionary_raum': filing.get('discretionary_raum'),
        'num_clients': filing.get('num_clients'),
        'client_types': filing.get('client_types', ''),
        'private_fund_count': filing.get('private_fund_count'),
        'adviser_kind': filing.get('adviser_kind', ''),
        'matched_query': filing.get('matched_query', ''),
        'sector_fit_score': score,
        'sector_fit_reason': reason,
        'excluded_reason': exclusion or '',
        'scraped_at': cfg['run_started_at'],
    }

    records: list[dict] = []
    if cfg['outputMode'] in ('firm', 'both'):
        firm = dict(base)
        firm['record_type'] = 'firm'
        firm['person_full_name'] = ''
        firm['person_first_name'] = ''
        firm['person_last_name'] = ''
        firm['person_title'] = ''
        firm['person_relationship'] = ''
        firm['person_email'] = filing.get('person_email', '')
        firm['dedupe_key'] = scoring.firm_dedupe_key(
            base['firm_name'], base['address_state']
        )
        records.append(firm)

    if cfg['outputMode'] in ('person', 'both'):
        # Form ADV gives us one contact (the CCO); Form D gives us the
        # related-persons list.
        candidates = people or (
            [
                {
                    'person_full_name': filing.get('person_full_name', ''),
                    'person_first_name': '',
                    'person_last_name': '',
                    'person_title': filing.get('person_title', ''),
                    'person_relationship': '',
                }
            ]
            if filing.get('person_full_name')
            else []
        )
        for person in candidates:
            if not person.get('person_full_name'):
                continue
            row = dict(base)
            row['record_type'] = 'person'
            row.update(
                {
                    'person_full_name': person.get('person_full_name', ''),
                    'person_first_name': person.get('person_first_name', ''),
                    'person_last_name': person.get('person_last_name', ''),
                    'person_title': person.get('person_title', ''),
                    'person_relationship': person.get('person_relationship', ''),
                    'person_email': filing.get('person_email', ''),
                }
            )
            row['persona_hint'] = scoring.persona_hint('person', source, score_text)
            row['dedupe_key'] = scoring.person_dedupe_key(
                row['person_full_name'], base['firm_name']
            )
            records.append(row)

    return records


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}

        user_agent = (raw.get('secUserAgent') or '').strip()
        if not user_agent:
            raise ValueError(
                'secUserAgent is required. The SEC needs a descriptive '
                'User-Agent with a contact email, e.g. '
                '"Patriot Holdings jer@patriotholdings.com".'
            )

        today = date.today()
        cfg = {
            'run_started_at': today.isoformat(),
            'outputMode': raw.get('outputMode', 'both'),
            'industryGroups': set(
                raw.get('industryGroups') or scoring.REAL_ASSET_INDUSTRY_GROUPS
            ),
            'includeStates': {s.upper() for s in (raw.get('includeStates') or [])},
            'minOfferingAmount': raw.get('minOfferingAmount') or 0,
            'maxOfferingAmount': raw.get('maxOfferingAmount') or 0,
            'requirePooledFund': bool(raw.get('requirePooledFund')),
        }
        min_score = raw.get('minSectorFitScore')
        min_score = 0 if min_score is None else int(min_score)
        keep_excluded = bool(raw.get('keepExcludedForReview'))
        max_records = int(raw.get('maxRecords') or 5000)

        if min_score > 0 and 'formADV' in (raw.get('sources') or []):
            Actor.log.warning(
                'minSectorFitScore=%s will discard most Form ADV rows: adviser '
                'names rarely contain sector language, so they score 0. Use the '
                'RAUM and state filters to screen the ADV side instead.', min_score,
            )

        known_keys = {
            scoring.normalize_name(name) for name in (raw.get('excludeFirmNames') or [])
        }

        client = SecClient(user_agent)
        stats = {
            'form_d_filings_seen': 0,
            'form_d_filings_kept': 0,
            'adv_rows_seen': 0,
            'adv_rows_kept': 0,
            'records_pushed': 0,
            'dropped_by_filter': 0,
            'dropped_by_score': 0,
            'dropped_by_exclusion': 0,
            'dropped_as_duplicate': 0,
        }
        seen_keys: set[str] = set()
        pushed = 0

        async def emit(filing: dict, source: str) -> None:
            nonlocal pushed
            for record in _build_records(filing, source, cfg):
                if pushed >= max_records:
                    return
                if record['excluded_reason'] and not keep_excluded:
                    stats['dropped_by_exclusion'] += 1
                    continue
                if record['sector_fit_score'] < min_score and not keep_excluded:
                    stats['dropped_by_score'] += 1
                    continue
                key = f'{record["record_type"]}:{record["dedupe_key"]}'
                firm_key = scoring.normalize_name(record['firm_name'])
                if key in seen_keys or firm_key in known_keys:
                    stats['dropped_as_duplicate'] += 1
                    continue
                seen_keys.add(key)
                await Actor.push_data(record)
                pushed += 1
                stats['records_pushed'] = pushed

        sources = raw.get('sources') or ['formD', 'formADV']

        # ---- Form D --------------------------------------------------
        if 'formD' in sources:
            mode = raw.get('formDMode', 'daily_index')
            include_amendments = raw.get('includeAmendments', True)
            refs: list[dict] = []

            if mode == 'daily_index':
                refs = await form_d.discover_daily_index(
                    client, int(raw.get('lookbackDays') or 30), include_amendments
                )
            elif mode == 'full_text':
                lookback = int(raw.get('lookbackDays') or 365)
                refs = await form_d.discover_full_text(
                    client,
                    raw.get('fullTextQueries') or DEFAULT_QUERIES,
                    _iso(today - timedelta(days=lookback)),
                    _iso(today),
                    include_amendments,
                    int(raw.get('maxPerQuery') or 300),
                )
            elif mode == 'quarterly':
                for quarter in raw.get('quarters') or []:
                    for filing in await form_d.load_quarterly_dataset(client, quarter):
                        stats['form_d_filings_seen'] += 1
                        rejection = _passes_form_d_filters(filing, cfg)
                        if rejection:
                            stats['dropped_by_filter'] += 1
                            continue
                        stats['form_d_filings_kept'] += 1
                        await emit(filing, 'sec_form_d')
                refs = []
            else:
                raise ValueError(f'unknown formDMode: {mode}')

            # De-duplicate accession numbers before spending requests on them.
            unique: dict[str, dict] = {}
            for ref in refs:
                unique.setdefault(ref['accession'], ref)
            Actor.log.info('Form D: %s unique filings to fetch', len(unique))

            for ref in unique.values():
                if pushed >= max_records:
                    break
                filing = await form_d.fetch_filing(client, ref)
                if filing is None:
                    continue
                stats['form_d_filings_seen'] += 1
                rejection = _passes_form_d_filters(filing, cfg)
                if rejection:
                    stats['dropped_by_filter'] += 1
                    continue
                stats['form_d_filings_kept'] += 1
                await emit(filing, 'sec_form_d')

        # ---- Form ADV ------------------------------------------------
        if 'formADV' in sources and pushed < max_records:
            reports = await form_adv.find_latest_report_urls(client)
            wanted = raw.get('advFeeds') or ['registered', 'exempt']
            adv_min_raum = int(raw.get('advMinRaum') or 0)
            adv_max_raum = int(raw.get('advMaxRaum') or 0)
            adv_states = {s.upper() for s in (raw.get('advStates') or [])}

            for kind in wanted:
                url = reports.get(kind)
                if not url:
                    Actor.log.warning('no %s ADV report link found', kind)
                    continue
                rows = await form_adv.load_adv_rows(
                    client, url, kind, int(raw.get('advMaxRows') or 100_000)
                )
                for row in rows:
                    if pushed >= max_records:
                        break
                    stats['adv_rows_seen'] += 1
                    raum = row.get('raum_usd')
                    if adv_min_raum and (raum is None or raum < adv_min_raum):
                        stats['dropped_by_filter'] += 1
                        continue
                    if adv_max_raum and raum is not None and raum > adv_max_raum:
                        stats['dropped_by_filter'] += 1
                        continue
                    if adv_states and (row.get('address_state') or '').upper() not in adv_states:
                        stats['dropped_by_filter'] += 1
                        continue
                    stats['adv_rows_kept'] += 1
                    await emit(row, 'sec_form_adv')

        await client.aclose()
        await Actor.set_value('RUN_SUMMARY', stats)
        Actor.log.info('Run summary: %s', stats)
