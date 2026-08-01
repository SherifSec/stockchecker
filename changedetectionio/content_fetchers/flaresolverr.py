import asyncio
import json
import os
import re
from html.parser import HTMLParser

from flask_babel import lazy_gettext as _l
from loguru import logger

from changedetectionio.content_fetchers.base import Fetcher
from changedetectionio.content_fetchers.exceptions import EmptyReply, Non200ErrorCodeReceived

# Mirrors the phrase list in stock-not-in-stock.js so restock mode works without a browser.
# @todo upstream: the JS file notes "Pass these in so the same list can be used in non-JS fetchers"
_OUT_OF_STOCK_PHRASES = [
    ' أخبرني عندما يتوفر', '0 in stock', 'actuellement indisponible', 'agotado',
    'article épuisé', 'artikel zurzeit vergriffen', 'as soon as stock is available',
    "aucune offre n'est disponible", 'ausverkauft', 'available for back order',
    'awaiting stock', 'back in stock soon', 'back-order or out of stock',
    'backordered', 'backorder', 'benachrichtigt mich', 'binnenkort leverbaar',
    'brak na stanie', 'brak w magazynie', 'coming soon',
    'currently have any tickets for this', 'currently unavailable',
    'dieser artikel ist bald wieder verfügbar', 'dostępne wkrótce', 'en rupture',
    'esgotado', 'in kürze lieferbar', 'indisponible', 'indisponível',
    "isn't in stock right now", 'isnt in stock right now',
    'item is no longer available', "let me know when it's available",
    'mail me when available', 'message if back in stock', 'mevcut değil',
    'more on order', 'nachricht bei', 'nicht auf lager', 'nicht lagernd',
    'nicht lieferbar', 'nicht verfügbar', 'nicht vorrätig', 'nicht mehr lieferbar',
    'nicht zur verfügung', 'nie znaleziono produktów', 'niet beschikbaar',
    'niet leverbaar', 'niet op voorraad', 'no disponible',
    'no featured offers available', 'no longer available', 'no longer in stock',
    'no tickets available', 'non disponibile', 'non disponible', 'not available',
    'not currently available', 'not in stock', 'notify me when available',
    'notify me', 'notify when available', 'não disponível',
    'não estamos a aceitar encomendas', 'out of stock', 'out-of-stock',
    'plus disponible', 'prodotto esaurito', 'produkt niedostępny', 'rupture',
    'sold out', 'sold-out', 'stok habis', 'stok kosong', 'stok varian ini habis',
    'stokta yok', 'temporarily out of stock', 'temporarily unavailable',
    'there were no search results for', 'this item is currently unavailable',
    'tickets unavailable', 'tidak dijual', 'tidak tersedia', 'tijdelijk uitverkocht',
    'tiket tidak tersedia', 'to subscribe to back in stock', 'tükendi',
    'unavailable nearby', 'unavailable tickets', 'vergriffen', 'vorbestellen',
    'vorbestellung ist bald möglich',
    "we couldn't find any products that match",
    'we do not currently have an estimate of when this product will be back in stock.',
    "we don't currently have any",
    "we don't know when or if this item will be back in stock.",
    'we were not able to find a match', 'when this arrives in stock',
    'when this item is available to order', 'zur zeit nicht an lager',
    'épuisé', '品切れ', '已售', '已售完', '품절',
]

_IN_STOCK_RE = re.compile(
    r'(\d+\s+in\s+stock|add\s+to\s+cart|add\s+to\s+basket|in\s+stock|arrives\s+approximately)',
    re.IGNORECASE,
)


class _VisibleTextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping script/style/head/noscript."""
    _SKIP = {'script', 'style', 'head', 'noscript'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._depth == 0:
            text = data.strip()
            if text:
                self.parts.append(text)


_CURRENCY_SYMBOLS = {
    '£': 'GBP', '$': 'USD', '€': 'EUR', '¥': 'JPY',
    '₹': 'INR', '₩': 'KRW', '₽': 'RUB', 'kr': 'SEK',
}

_PRICE_RE = re.compile(r'([£$€¥₹₩₽])\s*([\d,]+\.?\d*)')


def _detect_price(html_content):
    """Extract the main product price from visible HTML text using regex.

    Returns (price_float, currency_str) or (None, None) if no unambiguous
    price can be determined.

    Strategy: the main selling price on a single-product page typically appears
    more than once (buy box + summary row), while RRP/P&P/related prices appear
    only once. We pick the most-frequently-occurring price; if there's a tie we
    can't decide and return None.
    """
    extractor = _VisibleTextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        pass

    text = ' '.join(extractor.parts)
    matches = _PRICE_RE.findall(text)
    if not matches:
        return None, None

    counts = {}
    symbols = {}
    for symbol, amount_str in matches:
        try:
            amount = float(amount_str.replace(',', ''))
            if amount > 0:
                counts[amount] = counts.get(amount, 0) + 1
                symbols[amount] = symbol
        except ValueError:
            pass

    if not counts:
        return None, None

    max_count = max(counts.values())
    winners = [amt for amt, cnt in counts.items() if cnt == max_count]

    if len(winners) != 1:
        return None, None

    amount = winners[0]
    return amount, _CURRENCY_SYMBOLS.get(symbols[amount])


def _detect_instock(html_content):
    """Python equivalent of stock-not-in-stock.js for static HTML fetchers.

    Returns the matched out-of-stock phrase, or 'Possibly in stock' when
    nothing conclusive is found — matching the JS return values exactly so
    the restock processor can consume it unchanged.
    """
    extractor = _VisibleTextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        pass

    full_text = ' '.join(extractor.parts).lower()

    if _IN_STOCK_RE.search(full_text):
        return 'Possibly in stock'

    for phrase in _OUT_OF_STOCK_PHRASES:
        if phrase in full_text:
            return phrase

    return 'Possibly in stock'


class fetcher(Fetcher):
    fetcher_description = _l("FlareSolverr - Cloudflare bypass")

    def __init__(self, proxy_override=None, custom_browser_connection_url=None, **kwargs):
        super().__init__(**kwargs)
        base_url = os.getenv('FLARESOLVERR_URL', '').rstrip('/')
        self.flaresolverr_url = f"{base_url}/v1"

    def is_ready(self):
        return bool(os.getenv('FLARESOLVERR_URL'))

    def _run_sync(self, url, timeout, request_headers, request_body, request_method,
                  ignore_status_codes=False, current_include_filters=None,
                  is_binary=False, empty_pages_are_a_change=False, watch_uuid=None):
        import requests as req_lib

        max_timeout_ms = int((timeout or 60) * 1000)
        cmd = "request.post" if request_method and request_method.upper() == "POST" else "request.get"

        payload = {
            "cmd": cmd,
            "url": url,
            "maxTimeout": max_timeout_ms,
        }

        if request_headers:
            payload["headers"] = dict(request_headers)

        if cmd == "request.post" and request_body:
            payload["postData"] = request_body if isinstance(request_body, str) else request_body.decode("utf-8")

        logger.info(f"FlareSolverr: fetching {url}")

        try:
            r = req_lib.post(
                self.flaresolverr_url,
                json=payload,
                timeout=(timeout or 60) + 15,
            )
            r.raise_for_status()
        except Exception as e:
            raise Exception(f"FlareSolverr connection failed: {e}") from e

        data = r.json()

        if data.get("status") != "ok":
            raise Exception(f"FlareSolverr error: {data.get('message', 'Unknown error')}")

        solution = data["solution"]
        status_code = solution.get("status", 200)
        content = solution.get("response", "")

        if not content:
            if not empty_pages_are_a_change:
                raise EmptyReply(url=url, status_code=status_code)

        if status_code != 200 and not ignore_status_codes:
            raise Non200ErrorCodeReceived(url=url, status_code=status_code, page_html=content)

        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in solution.get("headers", {}).items()}
        self.instock_data = _detect_instock(content)
        logger.debug(f"FlareSolverr: instock_data='{self.instock_data}' for {url}")

        price, currency = _detect_price(content)
        if price is not None:
            schema_availability = (
                "https://schema.org/InStock"
                if self.instock_data == 'Possibly in stock'
                else "https://schema.org/OutOfStock"
            )
            injected = json.dumps({
                "@context": "https://schema.org",
                "@type": "Product",
                "offers": {
                    "@type": "Offer",
                    "price": price,
                    "priceCurrency": currency or "USD",
                    "availability": schema_availability,
                },
            })
            content += f'\n<script type="application/ld+json">{injected}</script>'
            logger.debug(f"FlareSolverr: injected price={price} currency={currency} availability={schema_availability} for {url}")

        self.content = content

    async def run(self,
                  fetch_favicon=True,
                  current_include_filters=None,
                  empty_pages_are_a_change=False,
                  ignore_status_codes=False,
                  is_binary=False,
                  request_body=None,
                  request_headers=None,
                  request_method=None,
                  screenshot_format=None,
                  timeout=None,
                  url=None,
                  watch_uuid=None):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._run_sync(
                url=url,
                timeout=timeout,
                request_headers=request_headers,
                request_body=request_body,
                request_method=request_method,
                ignore_status_codes=ignore_status_codes,
                current_include_filters=current_include_filters,
                is_binary=is_binary,
                empty_pages_are_a_change=empty_pages_are_a_change,
                watch_uuid=watch_uuid,
            )
        )

    async def quit(self, watch=None):
        pass
