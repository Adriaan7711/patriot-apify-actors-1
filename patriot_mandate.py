"""Patriot Holdings mandate — the single source of truth for all seven Actors.

This exact file ships as `src/patriot_mandate.py` inside every Actor. When you
change the thesis, change it here in shared/ and copy it out to all seven
Actors; nothing else in any Actor encodes what Patriot does or does not want.

Contains: sector-fit scoring, hard exclusions, dedupe keys, persona mapping,
the shared flat output record, and a rate-limited HTTP client.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata

import httpx
from apify import Actor

# --- Sector thesis -------------------------------------------------------
# Positive signals: the asset classes Patriot actually buys.
POSITIVE_TERMS: dict[str, int] = {
    'manufactured housing': 30,
    'manufactured home': 30,
    'mobile home': 28,
    'mhc': 20,
    'mhp': 20,
    'rv resort': 14,
    'rv park': 14,
    'self storage': 28,
    'self-storage': 28,
    'storage': 16,
    'industrial': 22,
    'small bay': 26,
    'small-bay': 26,
    'flex industrial': 24,
    'light industrial': 22,
    'industrial outdoor storage': 28,
    'ios': 10,
    'outdoor storage': 24,
    'truck terminal': 18,
    'affordable housing': 18,
    'workforce housing': 18,
    'net lease': 12,
    'essential real estate': 14,
    'real asset': 12,
    'real estate': 8,
    'opportunity zone': 8,
    '1031': 20,
    'delaware statutory trust': 16,
    'dst': 10,
    'exchange': 6,
}

# Negative signals: mandates that will not fund Patriot's thesis.
NEGATIVE_TERMS: dict[str, int] = {
    'multifamily': -22,
    'multi-family': -22,
    'apartment': -18,
    'student housing': -14,
    'senior housing': -12,
    'farmland': -25,
    'agricultur': -20,
    'timberland': -18,
    'oil': -22,
    'gas': -18,
    'mineral': -20,
    'crypto': -35,
    'digital asset': -35,
    'blockchain': -35,
    'token': -30,
    'venture': -25,
    'seed fund': -25,
    'biotech': -30,
    'life science': -22,
    'pharma': -30,
    'cannabis': -25,
    'litigation finance': -20,
    'hotel': -12,
    'hospitality': -12,
    'data center': -10,
}

# Firms/structures the playbook excludes outright.
HARD_EXCLUDE_PATTERNS: tuple[str, ...] = (
    r'\bboavida\b',                      # named do-not-contact
    r'\bpension\b',
    r'\bretirement system\b',
    r'\bteachers.? retirement\b',
    r'\bemployees.? retirement\b',
    r'\bcalpers\b',
    r'\bcalstrs\b',
    r'\bsovereign wealth\b',
    r'\bblackstone\b',
    r'\bbrookfield\b',
    r'\bstarwood\b',
    r'\bkkr\b',
    r'\bapollo global\b',
    r'\bcarlyle\b',
    r'\bares management\b',
)

# Issuer names that read as an operating business rather than a capital source.
NON_ALLOCATOR_HINTS: tuple[str, ...] = (
    'brewing', 'restaurant', 'coffee', 'salon', 'clinic', 'dental',
    'staffing', 'trucking company', 'roofing', 'landscap',
)

# Form D industryGroupType values worth keeping for a real-assets raise.
REAL_ASSET_INDUSTRY_GROUPS: tuple[str, ...] = (
    'Commercial',
    'Construction',
    'REITS and Finance',
    'REITS & Finance',
    'Residential',
    'Other Real Estate',
    'Pooled Investment Fund',
    'Investing',
    'Other Banking and Financial Services',
)

_ENTITY_SUFFIXES = (
    ' llc', ' l.l.c.', ' inc', ' inc.', ' incorporated', ' lp', ' l.p.', ' llp',
    ' ltd', ' limited', ' corp', ' corp.', ' corporation', ' company', ' co',
    ' trust', ' fund', ' partners', ' partnership', ' holdings', ' group',
)


def normalize_name(value: str | None) -> str:
    """Lowercase, strip punctuation/entity suffixes — for dedupe, not display."""
    if not value:
        return ''
    text = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    # Stripping punctuation turns "L.L.C." into "l l c". Re-join runs of
    # single letters so dotted acronyms match their undotted form, otherwise
    # "Acme Capital, L.L.C." and "Acme Capital LLC" become two firms.
    text = re.sub(r'\b(?:[a-z] )+[a-z]\b', lambda m: m.group(0).replace(' ', ''), text)
    changed = True
    while changed:
        changed = False
        for suffix in _ENTITY_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def _term_matches(term: str, haystack: str) -> bool:
    """Short acronyms need word boundaries; longer phrases can match anywhere.

    Without this, "ios" fires on "studios" and "dst" on "midstream".
    """
    if len(term) <= 4:
        return re.search(rf'\b{re.escape(term)}\b', haystack) is not None
    return term in haystack


# US state normalisation. Different sources spell the same state differently
# ("Virginia" in Invest Clearly's JSON-LD, "VA" in Form D, "Virginia" spelled
# out in an FEA address). Dedupe keys must agree across all four Actors, so
# every state goes through here first.
_STATES: dict[str, str] = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'puerto rico': 'PR', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}


def normalize_state(value: str | None) -> str:
    """Return a two-letter state code, or '' when it cannot be resolved."""
    if not value:
        return ''
    text = value.strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _STATES.get(text.lower(), '')


def score_sector_fit(*fields: str | None) -> tuple[int, str]:
    """Score a record against the Patriot thesis. Returns (score, reason)."""
    haystack = ' '.join(f.lower() for f in fields if f)
    if not haystack:
        return 0, 'no text to score'
    score = 0
    hits: list[str] = []
    misses: list[str] = []
    for term, weight in POSITIVE_TERMS.items():
        if _term_matches(term, haystack):
            score += weight
            hits.append(term)
    for term, weight in NEGATIVE_TERMS.items():
        if _term_matches(term, haystack):
            score += weight
            misses.append(term)
    parts = []
    if hits:
        parts.append('fit: ' + ', '.join(sorted(hits)[:6]))
    if misses:
        parts.append('against: ' + ', '.join(sorted(misses)[:6]))
    return score, '; '.join(parts) or 'no thesis keywords matched'


def is_hard_excluded(*fields: str | None) -> str | None:
    """Return the reason string if the record hits an exclusion rule."""
    haystack = ' '.join(f.lower() for f in fields if f)
    if not haystack:
        return None
    for pattern in HARD_EXCLUDE_PATTERNS:
        if re.search(pattern, haystack):
            return f'exclusion rule: /{pattern}/'
    for hint in NON_ALLOCATOR_HINTS:
        if hint in haystack:
            return f'reads as operating business: "{hint}"'
    return None


def firm_dedupe_key(firm_name: str | None, state: str | None) -> str:
    return f'{normalize_name(firm_name)}|{normalize_state(state).lower()}'


def person_dedupe_key(full_name: str | None, firm_name: str | None) -> str:
    """Key on first + last name only.

    Directory listings carry credentials in the display name ("Jarl A.
    Abrahamson, CES(R)"), and the same person shows up without them elsewhere.
    Keying on the raw string would treat those as two people.
    """
    first, last, _ = split_person_name(full_name)
    person = normalize_name(f'{first} {last}'.strip()) or normalize_name(full_name)
    return f'{person}|{normalize_name(firm_name)}'


def persona_hint(record_type: str, source: str, text: str | None = None) -> str:
    """Map a record onto the four-persona framework used downstream."""
    blob = (text or '').lower()
    if source == 'sec_form_adv':
        if 'pooled investment vehicle' in blob or 'private fund' in blob:
            return 'Portfolio Constructor (FoF / discretionary allocator)'
        return 'Principal Allocator (RIA / multi-family office)'
    # Form D
    if any(term in blob for term in ('fund', 'capital', 'partners', 'advisors', 'management')):
        return 'Sponsor / Co-GP or LP-adjacent (Form D issuer)'
    return 'Form D related person (verify before outreach)'


# =========================================================================
# Asset-class tag handling (Invest Clearly and any other tagged source)
# =========================================================================

# Tags that keep a firm in scope, mapped to the canonical Patriot sector.
KEEP_ASSET_CLASSES: dict[str, str] = {
    'mobile home park': 'MHC',
    'mobile home': 'MHC',
    'manufactured housing': 'MHC',
    'storage': 'Self-storage',
    'self storage': 'Self-storage',
    'industrial': 'Industrial',
    'flex': 'Industrial',
    'ios': 'IOS',
    'outdoor storage': 'IOS',
    'land': 'IOS',
    'affordable housing': 'Affordable',
    'build to rent': 'Adjacent',
    'rv park': 'MHC-adjacent',
    'net lease': 'Adjacent',
    'mixed use': 'Adjacent',
}

# Tags that are not, on their own, a reason to keep a firm.
NEUTRAL_ASSET_CLASSES: frozenset[str] = frozenset(
    {
        'multifamily', 'apartment', 'office', 'retail', 'hotel', 'hospitality',
        'student housing', 'senior housing', 'medical office', 'data center',
        'debt', 'notes', 'fund of funds', 'other',
    }
)


def classify_asset_classes(tags: list[str] | None) -> dict:
    """Apply the keep-MHC/storage/industrial/IOS, drop-multifamily-only rule.

    Returns {matched, verdict, reason}. A firm that lists multifamily *among*
    other in-scope sectors is kept: the rule is "drop multifamily-ONLY", not
    "drop anyone who touches multifamily".
    """
    cleaned = [t.strip() for t in (tags or []) if t and t.strip()]
    if not cleaned:
        return {'matched': [], 'verdict': 'review', 'reason': 'no asset classes listed'}

    matched: list[str] = []
    for tag in cleaned:
        low = tag.lower()
        for needle, sector in KEEP_ASSET_CLASSES.items():
            if needle in low and sector not in matched:
                matched.append(sector)

    if matched:
        return {
            'matched': matched,
            'verdict': 'keep',
            'reason': 'in-scope sectors: ' + ', '.join(matched),
        }

    lowered = {t.lower() for t in cleaned}
    if lowered and all(any(n in t for n in NEUTRAL_ASSET_CLASSES) for t in lowered):
        return {
            'matched': [],
            'verdict': 'drop',
            'reason': 'only out-of-scope sectors: ' + ', '.join(sorted(cleaned)[:6]),
        }
    return {
        'matched': [],
        'verdict': 'review',
        'reason': 'unrecognised sectors: ' + ', '.join(sorted(cleaned)[:6]),
    }


# =========================================================================
# Shared output record — every Actor emits this same flat shape
# =========================================================================

RECORD_FIELDS: tuple[str, ...] = (
    'record_type', 'source', 'persona_hint', 'sector_fit_score', 'sector_fit_reason',
    'asset_classes', 'asset_class_verdict', 'excluded_reason', 'dedupe_key',
    'firm_name', 'firm_website', 'firm_linkedin', 'firm_description',
    'address_street', 'address_city', 'address_state', 'address_zip', 'address_country',
    'phone', 'email',
    'person_full_name', 'person_first_name', 'person_last_name', 'person_title',
    'person_credentials', 'person_linkedin',
    'aum', 'year_founded', 'rating', 'review_count', 'active_deals', 'markets',
    'event_name', 'event_url', 'event_year', 'event_role',
    'source_url', 'scraped_at',
)


def blank_record() -> dict:
    return {f: '' for f in RECORD_FIELDS}


def finalize(record: dict, *, score_fields: tuple[str, ...] = ('firm_name', 'firm_description')) -> dict:
    """Fill in scoring, exclusions and the dedupe key on a partially built row."""
    score, reason = score_sector_fit(*(record.get(f) or '' for f in score_fields))
    record['sector_fit_score'] = score
    record['sector_fit_reason'] = reason
    record['excluded_reason'] = (
        is_hard_excluded(record.get('firm_name'), record.get('firm_description')) or ''
    )
    if record.get('record_type') == 'person':
        record['dedupe_key'] = person_dedupe_key(
            record.get('person_full_name'), record.get('firm_name')
        )
    else:
        record['dedupe_key'] = firm_dedupe_key(
            record.get('firm_name'), record.get('address_state')
        )
    return record


def split_person_name(full_name: str | None) -> tuple[str, str, str]:
    """Split a display name into (first, last, credentials).

    Handles the shapes these directories actually use:
    "Jarl A. Abrahamson, CES(R)" -> ("Jarl", "Abrahamson", "CES(R)")
    """
    if not full_name:
        return '', '', ''
    name = full_name.strip()
    credentials = ''
    if ',' in name:
        name, _, tail = name.partition(',')
        credentials = tail.strip()
    parts = [p for p in name.replace(' ', ' ').split() if p]
    if not parts:
        return '', '', credentials
    if len(parts) == 1:
        return parts[0], '', credentials
    return parts[0], parts[-1], credentials


# =========================================================================
# Rate-limited HTTP client
# =========================================================================

class PoliteClient:
    """One throttled, retrying HTTP client per Actor run.

    Defaults are deliberately gentle: these are association directories and
    conference sites, not APIs built for volume.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 2.0,
        timeout: float = 60.0,
    ) -> None:
        if not user_agent:
            raise ValueError('user_agent is required')
        self._min_interval = 1.0 / max(requests_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={'User-Agent': user_agent, 'Accept-Encoding': 'gzip, deflate'},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get_text(
        self,
        url: str,
        *,
        params: dict | None = None,
        attempts: int = 3,
        allow_error: bool = True,
    ) -> str | None:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                Actor.log.warning('GET %s failed (%s) %s/%s', url, exc, attempt, attempts)
            else:
                if response.status_code == 404:
                    Actor.log.info('404 %s', url)
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    Actor.log.warning(
                        'GET %s -> %s, backing off %.0fs', url, response.status_code, delay
                    )
                else:
                    return response.text
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
        if allow_error:
            Actor.log.warning('giving up on %s', url)
            return None
        raise RuntimeError(f'GET {url} failed after {attempts} attempts')

    async def get_json(
        self, url: str, *, params: dict | None = None, attempts: int = 3
    ):
        """GET and parse JSON. Returns None on failure rather than raising, so
        one bad response cannot take down a long run."""
        import json as _json

        text = await self.get_text(url, params=params, attempts=attempts)
        if text is None:
            return None
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            Actor.log.warning('non-JSON response from %s', url)
            return None
