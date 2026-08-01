import asyncio
import os

from flask_babel import lazy_gettext as _l
from loguru import logger

from changedetectionio.content_fetchers.base import Fetcher
from changedetectionio.content_fetchers.exceptions import EmptyReply, Non200ErrorCodeReceived
from changedetectionio.content_fetchers.price_utils import (
    _detect_instock,
    _detect_price,
    build_instock_jsonld,
)


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
            content += build_instock_jsonld(price, currency, self.instock_data)
            logger.debug(f"FlareSolverr: injected price={price} currency={currency} for {url}")

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
