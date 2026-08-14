import json
import logging
import os
import re
import time
from decimal import Decimal, ROUND_DOWN
from typing import Iterable

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


INTERVAL_SECONDS = 16 * 60
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("trading-bot")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def make_alpaca_client() -> TradingClient:
    return TradingClient(
        api_key=required_env("ALPACA_API_KEY"),
        secret_key=required_env("ALPACA_SECRET_KEY"),
        paper=env_bool("ALPACA_PAPER", True),
    )


def make_sheets_service():
    service_account_info = json.loads(required_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=GOOGLE_SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def parse_relative_volume(value) -> float | None:
    """Convert common Sheet values like 2.34, '2.34', '2,340', or '234%' to a sortable float."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(",", "")
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def get_ranked_symbols(sheets_service) -> list[tuple[str, float]]:
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")
    sheet_range = os.getenv("GOOGLE_SHEET_RANGE", "A:I")

    response = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheet_range)
        .execute()
    )
    rows = response.get("values", [])

    candidates: list[tuple[str, float]] = []
    for row in rows:
        if len(row) < 9:
            continue

        symbol = str(row[0]).strip().upper()
        relative_volume = parse_relative_volume(row[8])

        if not symbol or relative_volume is None:
            continue

        candidates.append((symbol, relative_volume))

    # Highest relative-volume rows first. De-dupe symbols, but keep the full
    # ranked list so we can continue downward whenever higher-ranked symbols
    # have already been bought.
    candidates.sort(key=lambda item: item[1], reverse=True)

    ranked: list[tuple[str, float]] = []
    seen: set[str] = set()
    for symbol, relative_volume in candidates:
        if symbol in seen:
            continue
        seen.add(symbol)
        ranked.append((symbol, relative_volume))

    return ranked


def cancel_open_orders(alpaca: TradingClient) -> None:
    responses = alpaca.cancel_orders()
    logger.info("Cancel-all requested for %d open order(s).", len(responses))


def held_symbols(alpaca: TradingClient) -> set[str]:
    return {position.symbol.upper() for position in alpaca.get_all_positions()}


def order_notional(alpaca: TradingClient) -> Decimal:
    account = alpaca.get_account()
    buying_power = Decimal(str(account.buying_power or "0"))

    amount = buying_power * Decimal("0.02")
    if amount < Decimal("1.00"):
        amount = Decimal("1.00")

    # Keep dollar orders to cents.
    return amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def submit_buy(alpaca: TradingClient, symbol: str) -> None:
    notional = order_notional(alpaca)

    order = MarketOrderRequest(
        symbol=symbol,
        notional=float(notional),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    submitted = alpaca.submit_order(order_data=order)
    logger.info(
        "Submitted BUY %s for $%s | order_id=%s",
        symbol,
        notional,
        submitted.id,
    )


def run_cycle(alpaca: TradingClient, sheets_service) -> None:
    logger.info("Starting 16-minute cycle.")

    # 1) Cancel anything that is still open/unfilled from prior cycles.
    cancel_open_orders(alpaca)

    # 2) Rank every valid Sheet row by relative volume in column I.
    ranked = get_ranked_symbols(sheets_service)
    logger.info("Found %d ranked sheet candidate(s).", len(ranked))

    # 3) Duplicate protection: never submit another buy for a symbol already held.
    # Keep moving down the ranking until five NEW symbols have been submitted.
    # Example: if ranks 1-5 are already held, ranks 6-10 become the five buys.
    already_held = held_symbols(alpaca)
    submitted_this_cycle: set[str] = set()
    buys_submitted = 0

    for symbol, relative_volume in ranked:
        if buys_submitted >= 5:
            break

        if symbol in already_held:
            logger.info(
                "Skipping %s (relative volume=%s): position already exists.",
                symbol,
                relative_volume,
            )
            continue

        if symbol in submitted_this_cycle:
            continue

        try:
            submit_buy(alpaca, symbol)
            submitted_this_cycle.add(symbol)
            buys_submitted += 1
        except Exception:
            # A rejected/bad symbol does not consume one of the five slots.
            # Continue down the ranking and try the next eligible symbol.
            logger.exception("Order failed for %s; trying the next ranked symbol.", symbol)

    logger.info("Submitted %d new buy order(s) this cycle.", buys_submitted)

    logger.info("Cycle complete.")


def main() -> None:
    alpaca = make_alpaca_client()
    sheets_service = make_sheets_service()

    logger.info(
        "Bot started | Alpaca mode=%s | interval=%d seconds",
        "PAPER" if env_bool("ALPACA_PAPER", True) else "LIVE",
        INTERVAL_SECONDS,
    )

    next_run = time.monotonic()

    while True:
        try:
            run_cycle(alpaca, sheets_service)
        except Exception:
            # Keep the Railway worker alive if one whole cycle fails.
            logger.exception("Cycle failed.")

        next_run += INTERVAL_SECONDS
        sleep_for = max(0, next_run - time.monotonic())
        logger.info("Next cycle in %.0f seconds.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
