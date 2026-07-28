import re
from urllib.parse import urlparse, parse_qs, unquote

from langdetect import detect, LangDetectException

from config import SEARCH_KEYWORDS, MARKET_BY_LANGUAGE

_YT_REDIRECT_RE = re.compile(
    r'https?://(?:www\.)?youtube\.com/redirect\?[^\s<>"]*',
    re.IGNORECASE,
)
_DIRECT_URL_RE = re.compile(
    r'https?://[^\s<>"()\[\]]+',
    re.IGNORECASE,
)

_SKIP_DOMAINS: set[str] = {
    "youtube.com", "youtu.be", "google.com", "goo.gl", "googleapis.com",
    "twitter.com", "x.com",
    "t.me", "telegram.me", "telegram.org",
    "instagram.com",
    "tiktok.com", "vm.tiktok.com",
    "facebook.com", "fb.com",
    "linkedin.com",
    "discord.gg", "discord.com",
    "reddit.com", "twitch.tv",
    "linktr.ee", "beacons.ai", "bio.link",
    "apps.apple.com", "play.google.com",
    "spotify.com", "amazon.com", "amzn.to",
}

# Noise words appended to exchange names in search keywords
_NOISE_RE = re.compile(
    r'\s+(?:exchange|global|spot|kripto|by\s+\w+)\s*$',
    re.IGNORECASE,
)
_REGION_RE = re.compile(r'\s+(?:us|eu|japan)\s*$', re.IGNORECASE)


def _parse_keyword(kw: str) -> tuple[str, str, str]:
    """
    Return (brand, brand_concat, display_name) for one SEARCH_KEYWORD.
      brand       – primary token used for domain boundary matching
      brand_concat – all alphanumeric chars joined (catches "mercado bitcoin" → "mercadobitcoin")
      display_name – clean title-cased name shown in Feishu
    """
    # Strip trailing pipe/bracket annotations:  "btcturk | kripto" → "btcturk"
    clean = re.sub(r'\s*[\|(\[].*$', '', kw).strip()
    clean = _NOISE_RE.sub('', clean).strip()
    clean = _REGION_RE.sub('', clean).strip()

    brand = clean.split()[0].lower().rstrip('.') if clean else kw.lower()
    brand_concat = re.sub(r'[^a-z0-9]', '', kw.lower())
    display = clean.title()
    return brand, brand_concat, display


def _build_platforms(keywords: list[str]) -> list[tuple[str, str, str]]:
    """
    Produce a deduped, longest-first list of (brand, brand_concat, display_name).
    Domain-style keywords like "crypto.com" keep their dot; all others need len >= 3.
    """
    seen: set[str] = set()
    result: list[tuple[str, str, str]] = []
    for kw in keywords:
        brand, concat, display = _parse_keyword(kw)
        if not brand or brand in seen:
            continue
        is_domain_style = '.' in brand
        if not is_domain_style and len(brand) < 3:
            continue
        seen.add(brand)
        result.append((brand, concat, display))
    result.sort(key=lambda x: -(len(x[0]) + len(x[1])))
    return result


_PLATFORMS = _build_platforms(SEARCH_KEYWORDS)


def _netloc(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _match_platform(host: str) -> str:
    """Return the display name of the first matching platform, or '' if none."""
    for brand, concat, display in _PLATFORMS:
        matched = False
        if '.' in brand:
            # Domain-style keyword: exact match or subdomain
            matched = host == brand or host.endswith('.' + brand)
        else:
            # Brand boundary: must appear adjacent to a dot or hyphen
            matched = (
                host.startswith(brand + '.') or
                host.startswith(brand + '-') or
                ('.' + brand + '.') in host or
                ('-' + brand + '.') in host
            )
        # Fallback: concatenated form for compound names (e.g. "mercado bitcoin")
        if not matched and len(concat) >= 5:
            matched = (
                host.startswith(concat + '.') or
                ('.' + concat + '.') in host
            )
        if matched:
            return display
    return ""


def _should_skip(url: str) -> bool:
    host = _netloc(url)
    if not host:
        return True
    return host in _SKIP_DOMAINS or any(host.endswith('.' + d) for d in _SKIP_DOMAINS)


def _resolve_yt_redirect(url: str) -> str:
    try:
        parsed = urlparse(url)
        q_values = parse_qs(parsed.query).get("q", [])
        return unquote(q_values[0]) if q_values else ""
    except Exception:
        return ""


def extract_promo_links(description: str) -> list[dict]:
    """
    Extract promo links for exchange platforms found in a video description.
    Platform list is derived dynamically from config.SEARCH_KEYWORDS.
    Returns: [{promo_link, promo_platform}, ...]
    """
    results: list[dict] = []
    seen: set[str] = set()

    def _emit(url: str):
        url = url.rstrip(".,;:)")
        if not url or url in seen or _should_skip(url):
            return
        platform = _match_platform(_netloc(url))
        if not platform:
            return
        seen.add(url)
        results.append({"promo_link": url, "promo_platform": platform})

    for match in _YT_REDIRECT_RE.finditer(description):
        actual = _resolve_yt_redirect(match.group(0))
        if actual:
            _emit(actual)

    for match in _DIRECT_URL_RE.finditer(description):
        url = match.group(0)
        if "youtube.com" in url or "youtu.be" in url:
            continue
        _emit(url)

    return results


# ── Channel-level enrichment: contact info, language, market ──────────────

_SOCIAL_PATTERNS: dict[str, re.Pattern] = {
    "twitter": re.compile(
        r'https?://(?:www\.)?(?:twitter|x)\.com/(?!intent/|home$|share|hashtag|search)[^\s<>"()\[\]]+',
        re.IGNORECASE,
    ),
    "telegram": re.compile(
        r'https?://(?:t|telegram)\.me/[^\s<>"()\[\]]+',
        re.IGNORECASE,
    ),
    "instagram": re.compile(
        r'https?://(?:www\.)?instagram\.com/[^\s<>"()\[\]]+',
        re.IGNORECASE,
    ),
    "tiktok": re.compile(
        r'https?://(?:(?:www\.|vm\.|vt\.)?tiktok\.com)[^\s<>"()\[\]]+',
        re.IGNORECASE,
    ),
    "facebook": re.compile(
        r'https?://(?:www\.)?(?:facebook|fb)\.com/[^\s<>"()\[\]]+',
        re.IGNORECASE,
    ),
}

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_EMAIL_SKIP_DOMAINS = {"youtube.com", "google.com", "example.com", "youtu.be", "gmail.com"}


def extract_social_links(text: str) -> dict[str, str]:
    """Return first URL found per social platform. Keys: twitter/telegram/instagram/tiktok/facebook."""
    result: dict[str, str] = {}
    for platform, pattern in _SOCIAL_PATTERNS.items():
        m = pattern.search(text)
        if m:
            result[platform] = m.group(0).rstrip(".,;:)")
    return result


def extract_emails(text: str) -> list[str]:
    """Extract unique non-system email addresses from text."""
    seen: set[str] = set()
    emails: list[str] = []
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0).lower()
        domain = email.split("@")[1]
        if domain not in _EMAIL_SKIP_DOMAINS and email not in seen:
            emails.append(email)
            seen.add(email)
    return emails


_HASHTAG_RE = re.compile(r'#[^\s#]+')


def extract_hashtags(text: str) -> list[str]:
    """
    Unique hashtags (lowercased, first-seen order) from free text. On YouTube
    these live almost entirely in the description (a tag block), not the
    title — this must be called on description text (or title+description)
    to find anything real; title alone returns near-nothing in practice.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for m in _HASHTAG_RE.finditer(text):
        tag = m.group(0).lower()
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def detect_language(text: str) -> str:
    """Best-effort ISO 639-1 language code for a video's title+description, '' if undetectable."""
    text = text.strip()
    if not text:
        return ""
    try:
        return detect(text)
    except LangDetectException:
        return ""


def classify_market(country: str, language: str) -> str:
    """
    Best-effort market for a channel — not restricted to a curated shortlist.
    `country` (real channel country from channels.list) always wins when it
    looks like a valid ISO 3166-1 alpha-2 code; `language` (detected from
    title+description) is only a fallback for channels the API didn't report
    a country for. '' only when neither signal is available.
    """
    country = (country or "").strip().upper()
    if len(country) == 2 and country.isalpha():
        return country
    return MARKET_BY_LANGUAGE.get((language or "").lower(), "")


# ── Title-based platform fallback ──────────────────────────────────────────
# extract_promo_links only looks at links in the description — a video whose
# title plainly names a known exchange but whose description has no matchable
# link (or no link at all) still shouldn't read as "unmatched". This scans
# free text (typically the title) for a known brand as a standalone word,
# reusing the same brand list _match_platform uses for URLs.

_TITLE_WORD_RE = re.compile(r'[a-z0-9]+', re.IGNORECASE)


def match_platform_in_text(text: str) -> str:
    """Return the display name of the first known platform brand named in
    free text (word-boundary match), or '' if none is found."""
    if not text:
        return ""
    words = {w.lower() for w in _TITLE_WORD_RE.findall(text)}
    text_lower = text.lower()
    for brand, concat, display in _PLATFORMS:
        if '.' in brand:
            if brand in text_lower:
                return display
        elif brand in words:
            return display
    return ""
