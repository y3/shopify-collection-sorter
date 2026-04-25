#!/usr/bin/env python3
"""
Shopify OOS Sorter
------------------
Sets every collection to manual sort order and moves out-of-stock products
to the end. Runs continuously on a configurable interval.

Usage:
    python oos_to_end.py

Configuration via environment variables (see .env.example):
    SHOPIFY_SHOP_URL      your-store.myshopify.com
    SHOPIFY_CLIENT_ID     custom app client ID
    SHOPIFY_CLIENT_SECRET custom app client secret
    INTERVAL_SECONDS      seconds between runs (default: 1800)
"""

import logging
import os
import time
from dataclasses import dataclass

import requests
from colorama import Fore, Back, Style, init as _colorama_init
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Colored logging
# ---------------------------------------------------------------------------

_colorama_init(autoreset=False)
_R = Style.RESET_ALL

_LEVEL_STYLES: dict[int, tuple[str, str]] = {
    #                       level badge              message
    logging.DEBUG:    (Style.DIM  + Fore.WHITE,      Style.DIM),
    logging.INFO:     (Style.BRIGHT + Fore.CYAN,     ""),
    logging.WARNING:  (Style.BRIGHT + Fore.YELLOW,   Fore.YELLOW),
    logging.ERROR:    (Style.BRIGHT + Fore.RED,      Fore.RED),
    logging.CRITICAL: (Back.RED + Fore.WHITE + Style.BRIGHT, Style.BRIGHT + Fore.RED),
}


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        badge_style, msg_style = _LEVEL_STYLES.get(record.levelno, ("", ""))

        ts    = Style.DIM + self.formatTime(record, "%H:%M:%S") + _R
        badge = badge_style + f"  {record.levelname:<8}" + _R
        msg   = msg_style + record.getMessage() + _R

        line = f"{ts}{badge}  {msg}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_ColorFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])


_setup_logging()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    shop_url: str
    client_id: str
    client_secret: str
    interval_seconds: int = 1800
    api_version: str = "2026-01"
    batch_size: int = 250

    @classmethod
    def from_env(cls) -> "Config":
        missing = [
            var for var in ("SHOPIFY_SHOP_URL", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")
            if not os.getenv(var)
        ]
        if missing:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            shop_url=os.environ["SHOPIFY_SHOP_URL"],
            client_id=os.environ["SHOPIFY_CLIENT_ID"],
            client_secret=os.environ["SHOPIFY_CLIENT_SECRET"],
            interval_seconds=int(os.getenv("INTERVAL_SECONDS", "1800")),
        )


# ---------------------------------------------------------------------------
# Shopify API client
# ---------------------------------------------------------------------------

class ShopifyClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._session = requests.Session()
        self._api_url = (
            f"https://{config.shop_url}/admin/api/{config.api_version}/graphql.json"
        )

    def authenticate(self) -> None:
        """Fetch a fresh access token and inject it into the session headers."""
        resp = self._session.post(
            f"https://{self.config.shop_url}/admin/oauth/access_token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self._session.headers.update({
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        })
        log.debug("Authenticated with Shopify")

    def gql(self, query: str, variables: dict | None = None, retries: int = 3) -> dict:
        """Execute a GraphQL request with retry and throttle handling."""
        for attempt in range(retries):
            resp = self._session.post(
                self._api_url,
                json={"query": query, "variables": variables or {}},
                timeout=30,
            )

            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limited — waiting %ds", wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            body = resp.json()

            # Back off if the GraphQL cost budget is running low
            throttle = body.get("extensions", {}).get("cost", {}).get("throttleStatus", {})
            if throttle.get("currentlyAvailable", 1000) < 100:
                restore_rate = throttle.get("restoreRate", 50)
                time.sleep(100 / restore_rate)

            if "errors" in body:
                raise RuntimeError(f"GraphQL error: {body['errors']}")

            return body["data"]

        raise RuntimeError(f"Request failed after {retries} retries")


# ---------------------------------------------------------------------------
# Sorting logic
# ---------------------------------------------------------------------------

class OOSSorter:
    _SET_MANUAL = """
    mutation ($id: ID!) {
        collectionUpdate(input: { id: $id, sortOrder: MANUAL }) {
            collection { id sortOrder }
            userErrors { field message }
        }
    }
    """

    _GET_PRODUCTS = """
    query ($id: ID!, $cursor: String) {
        collection(id: $id) {
            products(first: 250, after: $cursor) {
                edges {
                    cursor
                    node {
                        id
                        title
                        totalInventory
                        createdAt
                    }
                }
                pageInfo { hasNextPage }
            }
        }
    }
    """

    _REORDER = """
    mutation ($id: ID!, $moves: [MoveInput!]!) {
        collectionReorderProducts(id: $id, moves: $moves) {
            job { id }
            userErrors { field message }
        }
    }
    """

    def __init__(self, client: ShopifyClient) -> None:
        self.client = client

    # --- private helpers ---

    @staticmethod
    def _is_oos(product: dict) -> bool:
        inv = product["totalInventory"]
        return inv is not None and inv <= 0

    def _fetch_collections(self) -> list[dict]:
        collections: list[dict] = []
        cursor: str | None = None
        while True:
            after = f', after: "{cursor}"' if cursor else ""
            data = self.client.gql(f"""
                query {{
                    collections(first: 250{after}) {{
                        edges {{
                            cursor
                            node {{ id title sortOrder }}
                        }}
                        pageInfo {{ hasNextPage }}
                    }}
                }}
            """)["collections"]
            for edge in data["edges"]:
                collections.append(edge["node"])
            if not data["pageInfo"]["hasNextPage"]:
                break
            cursor = data["edges"][-1]["cursor"]
        return collections

    def _fetch_products(self, collection_id: str) -> list[dict]:
        products: list[dict] = []
        cursor: str | None = None
        while True:
            data = self.client.gql(
                self._GET_PRODUCTS, {"id": collection_id, "cursor": cursor}
            )["collection"]
            if not data:
                break
            page = data["products"]
            for edge in page["edges"]:
                products.append(edge["node"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["edges"][-1]["cursor"]
        return products

    def _set_manual_sort(self, collection_id: str) -> bool:
        result = self.client.gql(self._SET_MANUAL, {"id": collection_id})
        errors = result["collectionUpdate"]["userErrors"]
        if errors:
            log.error("Failed to set MANUAL sort: %s", errors)
            return False
        return True

    def _reorder_oos_to_end(self, collection_id: str, products: list[dict]) -> bool:
        in_stock = sorted(
            (p for p in products if not self._is_oos(p)),
            key=lambda p: p["createdAt"],
            reverse=True,
        )
        oos = sorted(
            (p for p in products if self._is_oos(p)),
            key=lambda p: p["createdAt"],
            reverse=True,
        )

        if not oos:
            return False

        moves = [
            {"id": p["id"], "newPosition": str(i)}
            for i, p in enumerate(in_stock + oos)
        ]

        batch_size = self.client.config.batch_size
        for i in range(0, len(moves), batch_size):
            result = self.client.gql(
                self._REORDER,
                {"id": collection_id, "moves": moves[i : i + batch_size]},
            )
            errors = result["collectionReorderProducts"]["userErrors"]
            if errors:
                log.error("Reorder error: %s", errors)
                return False
            if i + batch_size < len(moves):
                time.sleep(0.5)

        return True

    # --- public interface ---

    def run_once(self) -> None:
        """Process every collection once: ensure manual sort, push OOS to end."""
        log.info("Fetching collections...")
        collections = self._fetch_collections()
        log.info("Found %d collections", len(collections))

        for collection in collections:
            col_id: str = collection["id"]
            title: str = collection["title"]

            if collection["sortOrder"] != "MANUAL":
                log.info("[%s] Setting sort order → MANUAL", title)
                if not self._set_manual_sort(col_id):
                    continue

            products = self._fetch_products(col_id)
            oos_count = sum(1 for p in products if self._is_oos(p))

            if oos_count == 0:
                log.info("[%s] %d products, 0 OOS — skipping", title, len(products))
                continue

            log.info("[%s] %d products, %d OOS — reordering", title, len(products), oos_count)
            if self._reorder_oos_to_end(col_id, products):
                log.info("[%s] Done", title)

            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    config = Config.from_env()
    client = ShopifyClient(config)
    sorter = OOSSorter(client)

    interval_min = config.interval_seconds // 60
    log.info("OOS Sorter started — interval: %d min", interval_min)

    while True:
        log.info("=== Run started ===")
        try:
            client.authenticate()
            sorter.run_once()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            return
        except Exception:
            log.exception("Run failed")

        log.info("=== Run complete — next run in %d min ===", interval_min)
        try:
            time.sleep(config.interval_seconds)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            return


if __name__ == "__main__":
    main()
