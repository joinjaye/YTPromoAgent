import re
from urllib.parse import urlparse, parse_qs, unquote
from config import SEARCH_KEYWORDS

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
