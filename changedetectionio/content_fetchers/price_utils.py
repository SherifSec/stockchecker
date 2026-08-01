"""
Shared price and stock detection utilities used by multiple content fetchers.

Mirrors the phrase list in stock-not-in-stock.js so restock mode works without
a browser. The JS file notes: "Pass these in so the same list can be used in
non-JS fetchers" — this is that shared list.
"""

import re
from html.parser import HTMLParser


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
    'see all buying options',
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

_CURRENCY_SYMBOLS = {
    '£': 'GBP', '$': 'USD', '€': 'EUR', '¥': 'JPY',
    '₹': 'INR', '₩': 'KRW', '₽': 'RUB', 'kr': 'SEK',
}

_PRICE_RE = re.compile(r'([£$€¥₹₩₽])\s*([\d,]+\.?\d*)')


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


def _detect_price(html_content):
    """Extract the main product price from visible HTML text using regex.

    Strategy: the main selling price on a single-product page typically appears
    more than once (buy box + summary row), while RRP/P&P/related prices appear
    only once. Pick the most-frequently-occurring price; tie → None.

    Returns (price_float, currency_str) or (None, None).
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


def build_instock_jsonld(price, currency, instock_data):
    """Return an HTML snippet with injected JSON-LD for price+availability.

    Only call this when a meaningful price is available. The availability field
    is derived from instock_data: 'Possibly in stock' → InStock, anything else
    → OutOfStock.
    """
    import json
    schema_availability = (
        "https://schema.org/InStock"
        if instock_data == 'Possibly in stock'
        else "https://schema.org/OutOfStock"
    )
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": currency or "USD",
            "availability": schema_availability,
        },
    }
    return f'\n<script type="application/ld+json">{json.dumps(payload)}</script>'
