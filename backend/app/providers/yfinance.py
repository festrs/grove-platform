import logging
import math
import re
from decimal import Decimal

import yfinance as yf

from app.money import Money, Currency
from app.providers.common import DividendRecord

logger = logging.getLogger(__name__)

# Exchange codes we accept in search results. SAO = B3 (São Paulo); the
# rest are the US tape codes Yahoo emits for NASDAQ/NYSE/AMEX listings.
# Anything else (BUE, EBS, GER, …) is a foreign listing of a known issuer
# and would confuse the iOS asset-class detection.
_ALLOWED_EXCHANGES = {"SAO", "NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "OPR"}
_US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "OPR"}

# B3 tickers are 4 alphanumeric chars + 1-2 digits. Variants like PETR4F / PETR4Q
# (forwards/options) leak into yf.Search and must be dropped.
# The prefix allows digits to handle tickers like B3SA3 (B3 S.A.).
_B3_TICKER_RE = re.compile(r"^[A-Z0-9]{4}\d{1,2}$")
_BDR_SUFFIXES = ("32", "33", "34", "35", "39")

# iOS AssetClassType.rawValue → the set of apiType values _map_quote produces
# for that class. Used to filter when the caller scopes search to one class.
_CLASS_API_TYPES: dict[str, set[str]] = {
    "acoesBR": {"stock"},
    "fiis": {"fund"},
    "usStocks": {"common stock", "bdr", "etf"},
    "reits": {"reit"},
}
# Classes yfinance can't serve — caller should route elsewhere or skip.
_NON_YFINANCE_CLASSES = {"crypto", "rendaFixa"}

# Treasury/bond ETF names always carry one of these tokens; equity ETFs
# (VOO, SPY, QQQ, VTI) never do. The search payload has no category field,
# so this name heuristic is what routes a US-listed ETF to fixed income
# (rendaFixa) vs US Stocks on the iOS side.
_BOND_ETF_KEYWORDS = ("bond", "treasury", "fixed income", "t-bill")


def _is_bond_etf(name: str) -> bool:
    """True when an ETF's name marks it as a treasury/bond fund (SGOV, BND…)."""
    lowered = name.lower()
    return any(kw in lowered for kw in _BOND_ETF_KEYWORDS)


class YFinanceProvider:
    def search(
        self,
        query: str,
        max_results: int = 15,
        asset_class: str | None = None,
    ) -> list[dict]:
        """Search Yahoo Finance for stocks/FIIs/REITs (no crypto).

        ``asset_class`` is iOS ``AssetClassType.rawValue``. When set, results
        are filtered to that class and — for BR classes — the bare query is
        retried with a ``.SA`` suffix to surface B3 ticker matches Yahoo
        otherwise misses (e.g. typing "BTLG11" alone). Returns ``[]`` when
        the class isn't yfinance-served (crypto, rendaFixa).

        Each result: ``{symbol, name, type, sector, industry}`` where
        ``type`` ∈ ``{"stock", "fund", "bdr", "reit", "common stock",
        "etf", "fixed income"}``.
        """
        if asset_class in _NON_YFINANCE_CLASSES:
            return []

        queries = [query]
        if asset_class in {"acoesBR", "fiis"} and not query.upper().endswith(".SA"):
            queries.append(f"{query}.SA")

        seen_symbols: set[str] = set()
        results: list[dict] = []
        allowed_types = _CLASS_API_TYPES.get(asset_class) if asset_class else None

        for q in queries:
            try:
                search = yf.Search(q, max_results=max_results, enable_fuzzy_query=True)
                quotes = search.quotes or []
            except Exception:
                logger.warning("yfinance search failed for %s", q, exc_info=True)
                continue

            for raw in quotes:
                mapped = self._map_quote(raw)
                if mapped is None:
                    continue
                if allowed_types is not None and mapped["type"] not in allowed_types:
                    continue
                if mapped["symbol"] in seen_symbols:
                    continue
                seen_symbols.add(mapped["symbol"])
                results.append(mapped)
                if len(results) >= max_results:
                    return results
        return results

    @staticmethod
    def _map_quote(q: dict) -> dict | None:
        # EQUITY = single stocks/FIIs/BDRs; ETF = US-listed funds (SGOV, VOO).
        # Everything else (futures, options, mutual funds, indices) is dropped.
        quote_type = q.get("quoteType")
        if quote_type not in ("EQUITY", "ETF"):
            return None

        exchange = q.get("exchange") or ""
        if exchange not in _ALLOWED_EXCHANGES:
            return None

        symbol = q.get("symbol") or ""
        if not symbol:
            return None

        longname = q.get("longname") or ""
        shortname = q.get("shortname") or ""
        # B3 noise (PETR4F, PETR4Q, …) lacks a longname; require it to keep
        # the SAO list clean while leaving US results unaffected.
        if exchange == "SAO" and not longname:
            return None

        sector = (q.get("sector") or "").strip()
        industry = (q.get("industry") or "").strip()
        name = longname or shortname or symbol

        if exchange == "SAO":
            # B3-listed ETFs (BOVA11, IVVB11, …) collide with the FII ticker
            # shape and have no reliable class signal here — keep dropping them.
            if quote_type != "EQUITY":
                return None
            if not symbol.endswith(".SA"):
                return None
            ticker = symbol[:-3]
            if not _B3_TICKER_RE.match(ticker):
                return None
            if ticker.endswith("11"):
                api_type = "fund"
            elif any(ticker.endswith(suf) for suf in _BDR_SUFFIXES):
                api_type = "bdr"
            else:
                api_type = "stock"
        elif quote_type == "ETF":
            # US-listed ETF: a treasury/bond fund (SGOV, BND, AGG, TLT) is
            # fixed income; every other ETF is treated as a US equity.
            api_type = "fixed income" if _is_bond_etf(name) else "etf"
        else:  # US equity
            if sector.lower() == "real estate" or industry.lower().startswith("reit"):
                api_type = "reit"
            else:
                api_type = "common stock"

        return {
            "symbol": symbol,
            "name": name,
            "type": api_type,
            "sector": sector or None,
            "industry": industry or None,
        }

    def get_quote(self, symbol: str) -> dict:
        """Fetch current quote via `Ticker.info`.

        Returns the standard provider envelope plus `dividend_yield` (percent,
        as Yahoo reports it — e.g. 7.96 for PETR4.SA). `dividend_yield` is
        None when Yahoo doesn't expose it (crypto, ETFs without distributions,
        recently-listed names).
        """
        info = yf.Ticker(symbol).info or {}
        price_raw = info.get("regularMarketPrice") or info.get("currentPrice")
        if price_raw is None:
            raise ValueError(f"yfinance returned no price for {symbol}")
        currency = Currency.from_code(info.get("currency") or "USD")
        name = info.get("longName") or info.get("shortName") or symbol
        market_cap_raw = info.get("marketCap") or 0
        dy_raw = info.get("dividendYield")
        dividend_yield = Decimal(str(dy_raw)) if dy_raw is not None else None
        return {
            "symbol": symbol,
            "name": name,
            "current_price": Money(Decimal(str(price_raw)), currency),
            "currency": currency,
            "market_cap": Money(Decimal(str(market_cap_raw)), currency),
            "dividend_yield": dividend_yield,
        }

    def get_history(self, symbol: str, period: str = "1mo") -> list[dict]:
        """Fetch historical daily prices for a symbol via yfinance."""
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period)

            if hist.empty:
                return []

            return [
                {
                    "date": ts.strftime("%Y-%m-%d"),
                    "close": Decimal(str(round(row["Close"], 2))),
                    "volume": int(row.get("Volume", 0)),
                }
                for ts, row in hist.iterrows()
            ]
        except Exception:
            logger.warning("Failed to fetch history for %s period=%s", symbol, period, exc_info=True)
            return []

    def get_splits(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        """Fetch stock split history for a symbol within a date range.

        Returns list of dicts with keys: date, fromFactor, toFactor.
        """
        try:
            from datetime import date

            start = date.fromisoformat(from_date)
            end = date.fromisoformat(to_date)
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="max", actions=True)

            if hist.empty or "Stock Splits" not in hist.columns:
                return []

            results = []
            for ts, row in hist.iterrows():
                ratio = row["Stock Splits"]
                if ratio <= 0:
                    continue
                split_date = ts.date()
                if split_date < start or split_date > end:
                    continue
                results.append({
                    "date": split_date.isoformat(),
                    "fromFactor": 1,
                    "toFactor": float(ratio),
                })
            return results
        except Exception:
            logger.warning("Failed to fetch splits for %s", symbol, exc_info=True)
            return []

    def get_dividends(self, symbol: str) -> list[DividendRecord]:
        """Fetch dividend history (past + announced upcoming when available).

        Past payments come from `ticker.history(actions=True)`. The next
        announced dividend (if any) is read from `ticker.info` and appended
        as a forward-dated record so the upcoming-dividends view can find it.
        """
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="max", actions=True)
            records: list[DividendRecord] = []

            if not hist.empty and "Dividends" in hist.columns:
                for ts, row in hist.iterrows():
                    amount = row["Dividends"]
                    if amount <= 0:
                        continue
                    ex_date = ts.date()
                    records.append(DividendRecord(
                        dividend_type="Dividend",
                        value=Decimal(str(amount)).quantize(Decimal("0.000001")),
                        record_date=ex_date,
                        ex_date=ex_date,
                        payment_date=None,
                    ))

            upcoming = self._get_upcoming_dividend(ticker)
            if upcoming is not None:
                records.append(upcoming)
            return records
        except Exception:
            logger.warning("Failed to fetch dividends for %s", symbol, exc_info=True)
            return []

    @staticmethod
    def _get_upcoming_dividend(ticker) -> "DividendRecord | None":
        """Read `ticker.info` for an announced upcoming dividend, if any.

        yfinance exposes the next ex-dividend date via `exDividendDate`
        (Unix epoch) and the per-share amount via `lastDividendValue`.
        We only return a record when the date is in the future to avoid
        duplicating past entries already pulled from history.
        """
        from datetime import date, datetime, timezone
        try:
            info = ticker.info or {}
        except Exception:
            return None

        ex_epoch = info.get("exDividendDate")
        amount = info.get("lastDividendValue")
        if not ex_epoch or amount is None or amount <= 0:
            return None

        try:
            ex_date = datetime.fromtimestamp(int(ex_epoch), tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            return None

        if ex_date < date.today():
            return None

        return DividendRecord(
            dividend_type="Dividend",
            value=Decimal(str(amount)).quantize(Decimal("0.000001")),
            record_date=ex_date,
            ex_date=ex_date,
            payment_date=None,
        )

    def get_fundamentals(self, symbol: str) -> dict:
        """Fetch fundamental financial data for scoring."""
        empty = {
            "ipo_years": None,
            "eps_history": [],
            "net_income_history": [],
            "debt_history": [],
            "current_net_debt_ebitda": None,
            "raw_data": [],
        }
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            # IPO years
            from datetime import datetime, timezone
            epoch = info.get("firstTradeDateEpochUtc") or info.get("firstTradeDate")
            epoch_ms = info.get("firstTradeDateMilliseconds")
            if epoch:
                ipo_date = datetime.fromtimestamp(epoch, tz=timezone.utc)
                ipo_years = (datetime.now(timezone.utc) - ipo_date).days // 365
            elif epoch_ms:
                ipo_date = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
                ipo_years = (datetime.now(timezone.utc) - ipo_date).days // 365
            else:
                ipo_years = None

            financials = ticker.financials
            balance_sheet = ticker.balance_sheet

            if financials is None or financials.empty:
                empty["ipo_years"] = ipo_years
                return empty

            # Sort columns chronologically (oldest first)
            sorted_cols = sorted(financials.columns)

            def _get_row(df, *labels):
                """Get row values for first matching label."""
                if df is None or df.empty:
                    return {}
                for label in labels:
                    if label in df.index:
                        return {col: df.loc[label, col] for col in sorted_cols if col in df.columns}
                return {}

            eps_row = _get_row(financials, "Diluted EPS", "Basic EPS")
            ni_row = _get_row(financials, "Net Income")
            ebitda_row = _get_row(financials, "EBITDA", "Operating Income")
            debt_row = _get_row(balance_sheet, "Long Term Debt", "Total Debt")

            def _safe_float(val: object) -> float:
                """Convert to float, treating NaN/None as 0."""
                if val is None:
                    return 0.0
                f = float(val)
                return 0.0 if math.isnan(f) else f

            eps_history = []
            net_income_history = []
            debt_history = []
            raw_data = []

            for col in sorted_cols:
                eps = _safe_float(eps_row.get(col))
                ni = _safe_float(ni_row.get(col))
                ebitda = _safe_float(ebitda_row.get(col))
                debt = _safe_float(debt_row.get(col))
                debt_ratio = (debt / ebitda) if ebitda != 0 else 0.0

                eps_history.append(eps)
                net_income_history.append(ni)
                debt_history.append(debt_ratio)

                year = col.year if hasattr(col, "year") else None
                raw_data.append({
                    "year": year,
                    "eps": eps,
                    "net_income": ni,
                    "net_debt_ebitda": round(debt_ratio, 4),
                })

            current_net_debt_ebitda = debt_history[-1] if debt_history else None

            return {
                "ipo_years": ipo_years,
                "eps_history": eps_history,
                "net_income_history": net_income_history,
                "debt_history": debt_history,
                "current_net_debt_ebitda": current_net_debt_ebitda,
                "raw_data": raw_data,
            }
        except Exception:
            logger.warning("Failed to fetch fundamentals for %s", symbol, exc_info=True)
            return empty
