"""
Enhanced Google Maps Client with pre-filtering, concurrency control, enrichment, and DOM-based extraction.
"""
import asyncio
import json
import logging
import re
import time
from urllib.parse import quote, urljoin, urlparse, parse_qs
from typing import Optional

from playwright.async_api import async_playwright as async_pw
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

PLAYWRIGHT_NAV_TIMEOUT = 45000

# Characters to strip from extracted text — PUA, glyphs, invisible chars
STRIP_CHARS_RE = re.compile(
    '[\uE000-\uF8FF'   # Private Use Area
    '\uFFF0-\uFFFF'     # Specials
    '\u200B-\u200F'     # Zero-width / direction marks
    '\uFEFF'            # BOM
    '\u00AD'            # Soft hyphen
    '\u2060-\u2069'     # Invisible operators
    ']'
)


def _strip_glyphs(text: str) -> str:
    return STRIP_CHARS_RE.sub('', text).strip()


def _parse_coords_from_url(place_url: str) -> dict:
    """Extract lat/lng from Google Maps place URL (!3d/!4d params)."""
    m = re.search(r'!3d([\d.-]+)!4d([\d.-]+)', place_url)
    if m:
        return {'lat': float(m.group(1)), 'lng': float(m.group(2))}
    return {}


def _parse_address_components(full_address: str) -> dict:
    """
    Parse a full address like '1600 W Lake St, Addison, IL 60101, United States'
    into components. Returns dict with street, city, state, zip, country.
    """
    result = {'street': '', 'city': '', 'state': '', 'zip': '', 'country': ''}
    if not full_address:
        return result

    parts = [p.strip() for p in full_address.split(',')]
    if len(parts) >= 1:
        result['street'] = parts[0]
    if len(parts) >= 2:
        result['city'] = parts[1]
    if len(parts) >= 3:
        state_zip = parts[2].strip().split()
        if len(state_zip) >= 1:
            result['state'] = state_zip[0]
        if len(state_zip) >= 2:
            zip_match = re.search(r'\b(\d{5}(?:-\d{4})?)\b', ' '.join(state_zip[1:]))
            if zip_match:
                result['zip'] = zip_match.group(1)
    if len(parts) >= 4:
        result['country'] = parts[-1]
    elif len(parts) == 3 and not re.match(r'^\d{5}', parts[2].strip()):
        result['country'] = parts[2].strip()

    return result


class EnhancedGoogleMapsClient:
    """
    Enhanced client with:
    - Pre-filtered search queries (website, rating)
    - Configurable concurrency
    - Optional browser-based enrichment
    - Rendered DOM extraction (Google Maps loads results async via XHR)
    - Full address extraction from detail pages
    """

    def __init__(self, max_concurrency: int = 5, use_proxy: bool = False):
        self.proxy = 'http://proxy.apify.com:8000' if use_proxy else None
        self.max_concurrency = min(max_concurrency, 7)
        self.playwright_browser = None
        self._playwright = None

    def _build_search_url(self, query: str) -> str:
        """Build Google Maps search URL."""
        q = quote(query)
        return f'https://www.google.com/maps/search/{q}/'

    def _apply_filters_to_query(self, query: str, website_selection: str, min_rating: str) -> str:
        """Apply Google Maps search filters to the query string."""
        if min_rating and min_rating != 'any':
            rating_map = {'3.0': '3', '3.5': '3.5', '4.0': '4', '4.5': '4.5'}
            rating_val = rating_map.get(min_rating, '')
            if rating_val:
                query = f'{query} rating:{rating_val}'

        if website_selection == 'with_website':
            query = f'{query} has:website'
        elif website_selection == 'without_website':
            query = f'{query} -has:website'

        return query

    async def fetch_search_page(self, query: str) -> str:
        """
        Load search page in Playwright, wait for results to render, return rendered HTML + extracted places.
        Returns JSON-encoded string with keys: html, places.
        """
        url = self._build_search_url(query)
        try:
            async with async_pw() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                page = await browser.new_page()
                await page.goto(url, wait_until='domcontentloaded', timeout=PLAYWRIGHT_NAV_TIMEOUT)
                await page.wait_for_timeout(3000)
                try:
                    await page.wait_for_selector('[role="feed"]', timeout=10000)
                except Exception:
                    logger.warning(f'Search results feed not found for: {query}')
                await page.wait_for_timeout(2000)
                html = await page.content()
                places = await self._extract_places_from_page(page)
                await browser.close()
                result = {'html': html, 'places': places}
                return json.dumps(result)
        except Exception as e:
            logger.error(f'Playwright search failed for "{query}": {e}')
            return json.dumps({'html': '', 'places': []})

    def _clean_line(self, line: str) -> str:
        """Strip glyphs and invisible chars from a line of text."""
        return _strip_glyphs(line)

    async def _extract_places_from_page(self, page) -> list:
        """Extract place data from rendered search results page DOM."""
        result = await page.evaluate('''() => {
            const strip = (s) => {
                if (!s) return s;
                return s.replace(/[\\uE000-\\uF8FF\\uFFF0-\\uFFFF\\u200B-\\u200F\\uFEFF\\u00AD\\u2060-\\u2069]/g, '').trim();
            };
            const articles = document.querySelectorAll('[role="article"]');
            const results = [];
            articles.forEach(art => {
                const card = {};
                const placeLink = art.querySelector('a[href*="/maps/place/"]');
                if (!placeLink) return;
                card.placeUrl = placeLink.href;
                card.title = strip(placeLink.getAttribute('aria-label'));
                if (!card.title) return;

                const text = art.innerText || '';
                const rawLines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                const lines = rawLines.map(l => strip(l)).filter(Boolean);

                // Extract rating — first float between 1-5
                for (const line of lines) {
                    const m = line.match(/^(\\d+\\.?\\d*)$/);
                    if (m) {
                        const v = parseFloat(m[1]);
                        if (v >= 1.0 && v <= 5.0) { card.totalScore = v; break; }
                    }
                }

                // Extract phone — pattern like +1 512-555-1234 or (512) 555-1234
                for (const line of lines) {
                    const phoneMatch = line.match(/([(+]?\\d[\\d\\s().-]{7,20}\\d)/);
                    if (phoneMatch && !line.includes('Website') && !line.includes('Directions')) {
                        card.phone = phoneMatch[1].trim();
                        break;
                    }
                }

                // Extract website URL
                const websiteLink = art.querySelector('a[href*="://"]:not([href*="google.com"])');
                if (websiteLink) {
                    const href = websiteLink.href;
                    if (href.startsWith('http') && !href.includes('google.com/aclk') && !href.includes('form.recreateai')) {
                        card.website = href;
                    }
                }

                // Category + Address: find the line after rating that has category info
                let foundRating = false;
                for (const line of lines) {
                    const lineNum = parseFloat(line);
                    if (!foundRating && card.totalScore && !isNaN(lineNum) && lineNum === card.totalScore) {
                        foundRating = true;
                        continue;
                    }
                    if (foundRating && line.includes('·')) {
                        const parts = line.split('·').map(s => strip(s)).filter(Boolean);
                        if (!card.category && parts.length > 0) card.category = parts[0];
                        if (parts.length > 1) {
                            card.address = parts.slice(1).join(', ');
                        }
                        break;
                    }
                    if (foundRating && !line.match(/^[\\d.]+$/) && !line.includes('Website') && !line.includes('Directions') && !line.includes('Book online') && line.length > 0) {
                        if (!card.category) {
                            const parts = line.split('·').map(s => strip(s)).filter(Boolean);
                            card.category = parts[0];
                            if (parts.length > 1) {
                                card.address = parts.slice(1).join(', ');
                            } else if (!card.address) {
                                card.address = line;
                            }
                        }
                        break;
                    }
                }

                // Safety net: if category or address still has · or glyphs, strip them
                if (card.category && card.category.includes('·')) {
                    card.category = card.category.split('·')[0].trim();
                }
                if (card.address) {
                    // Strip any remaining · prefix or glyph pieces
                    const addrParts = card.address.split('·').map(s => strip(s)).filter(Boolean);
                    card.address = addrParts.join(', ');
                }

                // Address fallback: if no address yet, look for city/state pattern
                if (!card.address) {
                    for (const line of lines) {
                        if (/\\d{5}(-\\d{4})?/.test(line)) {
                            card.address = line;
                            break;
                        }
                    }
                }
                if (!card.address) {
                    for (const line of lines) {
                        if (/,\\s*[A-Z]{2}(\\s|$)/.test(line) && !line.match(/^(Open|Closed)/i) && !line.includes('Website') && !line.includes('Directions')) {
                            card.address = line;
                            break;
                        }
                    }
                }
                if (!card.address) {
                    for (const line of lines) {
                        // Look for anything with at least 3 words that isn't title/rating/phone/website/directions
                        if (line.split(' ').length >= 3 && !line.match(/^[\\d.]+$/) && !line.includes('Website') && !line.includes('Directions') && !line.includes('Book online') && !line.match(/^(Open|Closed)/i) && line !== card.title) {
                            card.address = line;
                            break;
                        }
                    }
                }

                // Hours: search for "Open" or "Closed" in lines
                for (const line of lines) {
                    if (/^(Open|Closed)/i.test(line)) {
                        card.hours = line;
                        break;
                    }
                }

                // Reviews count: look for "(N)" or "N reviews" pattern
                const reviewEl = art.querySelector('[aria-label*="star"]');
                if (reviewEl) {
                    const ariaLabel = reviewEl.getAttribute('aria-label') || '';
                    const revMatch = ariaLabel.match(/(\\d+)\\s+review/);
                    if (revMatch) card.reviewsCount = parseInt(revMatch[1], 10);
                }

                // isClaimed: look for "Claimed" badge text in the card
                card.isClaimed = text.includes('Claimed') || text.includes('claimed');

                results.push(card);
            });
            return results;
        }''')
        return result or []

    async def extract_full_address(self, place_url: str, page=None, browser=None) -> dict:
        """
        Navigate to a place detail page and extract the full address + components.
        Returns dict with street, city, state, zip, country, and fullAddress.
        Accepts optional shared page or browser to avoid opening a new browser per place.
        """
        own_browser = False
        own_page = False
        try:
            if page is None and browser is None:
                pw = await async_pw().__aenter__()
                browser = await pw.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                page = await browser.new_page()
                own_browser = True

            if page is None and browser is not None:
                page = await browser.new_page()
                own_page = True

            await page.goto(place_url, wait_until='domcontentloaded', timeout=PLAYWRIGHT_NAV_TIMEOUT)
            await page.wait_for_timeout(3000)

            addr_info = await page.evaluate('''() => {
                const result = { fullAddress: '', street: '', city: '', state: '', zip: '', country: '' };
                const selectors = [
                    '[data-item-id="address"]',
                    'button[data-item-id*="address"]',
                    'button[aria-label*="Address"]',
                    '[role="main"] button[aria-label*="address"]',
                ];
                let addrEl = null;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim()) { addrEl = el; break; }
                    if (el && el.getAttribute('aria-label')) {
                        const al = el.getAttribute('aria-label').replace(/^Address:\\s*/i, '');
                        if (al) { result.fullAddress = al; break; }
                    }
                }
                if (!result.fullAddress && addrEl) {
                    result.fullAddress = addrEl.innerText.trim();
                }
                if (!result.fullAddress) {
                    const body = document.body.innerText || '';
                    const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);
                    for (const line of lines) {
                        if (/^\\d+\\s+\\w+/.test(line) && /,\\s*[A-Z]{2}\\s+\\d{5}/.test(line)) {
                            result.fullAddress = line;
                            break;
                        }
                    }
                }
                return result;
            }''')

            if addr_info.get('fullAddress'):
                parsed = _parse_address_components(addr_info['fullAddress'])
                addr_info['street'] = parsed.get('street', '') or addr_info.get('street', '')
                addr_info['city'] = parsed.get('city', '') or addr_info.get('city', '')
                addr_info['state'] = parsed.get('state', '') or addr_info.get('state', '')
                addr_info['zip'] = parsed.get('zip', '') or addr_info.get('zip', '')
                addr_info['country'] = parsed.get('country', '') or addr_info.get('country', '')

            if own_page:
                await page.close()
            if own_browser:
                await browser.close()

            return addr_info
        except Exception as e:
            logger.warning(f'Full address extraction failed for {place_url}: {e}')
            try:
                if own_page and page:
                    await page.close()
                if own_browser and browser:
                    await browser.close()
            except Exception:
                pass
            return {'fullAddress': '', 'street': '', 'city': '', 'state': '', 'zip': '', 'country': ''}

    async def fetch_place_page(self, place_url: str) -> str:
        """Fetch individual place page HTML using Playwright."""
        try:
            async with async_pw() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                page = await browser.new_page()
                await page.goto(place_url, wait_until='domcontentloaded', timeout=PLAYWRIGHT_NAV_TIMEOUT)
                await page.wait_for_timeout(3000)
                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            logger.error(f'Playwright place page fetch failed: {e}')
            return ''

    def start_browser(self):
        """Start Playwright browser for enrichment tasks."""
        if self.playwright_browser is None:
            self._playwright = sync_playwright().start()
            self.playwright_browser = self._playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
        return self.playwright_browser

    def close_browser(self):
        """Close Playwright browser."""
        if self.playwright_browser:
            self.playwright_browser.close()
        if self._playwright:
            self._playwright.stop()
        self.playwright_browser = None
        self._playwright = None

    async def async_start_browser(self):
        """Start async Playwright browser for shared use across multiple pages."""
        if not hasattr(self, '_async_playwright') or self._async_playwright is None:
            self._async_playwright = await async_pw().__aenter__()
            self._async_browser = await self._async_playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
        return self._async_browser

    async def async_close_browser(self):
        """Close async Playwright browser."""
        if hasattr(self, '_async_browser') and self._async_browser:
            await self._async_browser.close()
            self._async_browser = None
        if hasattr(self, '_async_playwright') and self._async_playwright:
            await self._async_playwright.__aexit__(None, None, None)
            self._async_playwright = None

    async def close(self):
        """Close all connections."""
        self.close_browser()
        await self.async_close_browser()