"""Tests for YFinanceProvider.search() — the unified non-crypto search.

The mapper has to be hermetic: every test mocks ``yf.Search`` so we never
hit Yahoo. The fixtures mirror real shapes captured during the live probe
that informed the implementation (see plan).
"""
from unittest.mock import patch, MagicMock

from app.providers.yfinance import YFinanceProvider


def _quote(**kwargs) -> dict:
    base = {
        "quoteType": "EQUITY",
        "exchange": "NMS",
        "symbol": "AAPL",
        "longname": "Apple Inc.",
        "shortname": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
    base.update(kwargs)
    return base


def _mock_search(results_per_query: dict | list):
    """Build a ``yf.Search`` MagicMock factory.

    Pass a dict to vary results by query string (``.SA`` retry tests need
    this), or a list to return the same quotes regardless of query.
    """
    def factory(query, **_):
        m = MagicMock()
        if isinstance(results_per_query, dict):
            m.quotes = results_per_query.get(query, [])
        else:
            m.quotes = results_per_query
        return m
    return factory


def test_us_equity_maps_to_common_stock():
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([_quote()])):
        out = p.search("AAPL")
    assert out == [{
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "type": "common stock",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }]


def test_us_real_estate_sector_maps_to_reit():
    p = YFinanceProvider()
    quote = _quote(
        symbol="O",
        longname="Realty Income Corporation",
        shortname="Realty Income Corporation",
        exchange="NYQ",
        sector="Real Estate",
        industry="REIT—Retail",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([quote])):
        out = p.search("O")
    assert len(out) == 1
    assert out[0]["type"] == "reit"


def test_sao_ticker_ending_11_maps_to_fund():
    p = YFinanceProvider()
    quote = _quote(
        symbol="BTLG11.SA",
        longname="BTG Pactual Logística FII",
        shortname="FII BTLG",
        exchange="SAO",
        sector="Real Estate",
        industry="REIT—Industrial",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([quote])):
        out = p.search("BTLG11")
    assert out and out[0]["type"] == "fund"


def test_sao_normal_ticker_maps_to_stock():
    p = YFinanceProvider()
    quote = _quote(
        symbol="PETR4.SA",
        longname="Petrobras",
        shortname="PETROBRAS",
        exchange="SAO",
        sector="Energy",
        industry="Oil & Gas Integrated",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([quote])):
        out = p.search("PETR4")
    assert out and out[0]["type"] == "stock"


def test_sao_bdr_suffix_maps_to_bdr():
    p = YFinanceProvider()
    quote = _quote(
        symbol="AAPL34.SA",
        longname="Apple BDR",
        shortname="AAPL BDR",
        exchange="SAO",
        sector="Technology",
        industry="Consumer Electronics",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([quote])):
        out = p.search("AAPL34")
    assert out and out[0]["type"] == "bdr"


def test_filters_drop_non_tradable_quote_types():
    """Futures/options/mutual funds/indices are dropped; EQUITY and ETF stay."""
    p = YFinanceProvider()
    futures = _quote(quoteType="FUTURE", symbol="CL=F", exchange="NYM")
    mutual = _quote(quoteType="MUTUALFUND", symbol="VFIAX", exchange="NAS")
    index = _quote(quoteType="INDEX", symbol="^GSPC", exchange="SNP")
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([futures, mutual, index, _quote()])):
        out = p.search("AAPL")
    assert [r["symbol"] for r in out] == ["AAPL"]


def test_us_bond_etf_maps_to_fixed_income():
    """A treasury/bond ETF (SGOV) routes to the fixed-income class."""
    p = YFinanceProvider()
    sgov = _quote(
        quoteType="ETF",
        symbol="SGOV",
        exchange="NYQ",
        longname="iShares 0-3 Month Treasury Bond ETF",
        shortname="ISHARES 0-3 MONTH TREASURY",
        sector="",
        industry="",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([sgov])):
        out = p.search("SGOV")
    assert len(out) == 1
    assert out[0]["symbol"] == "SGOV"
    assert out[0]["type"] == "fixed income"


def test_us_equity_etf_maps_to_etf():
    """A non-bond ETF (VOO) routes to US Stocks via the "etf" type."""
    p = YFinanceProvider()
    voo = _quote(
        quoteType="ETF",
        symbol="VOO",
        exchange="PCX",
        longname="Vanguard S&P 500 ETF",
        shortname="Vanguard S&P 500 ETF",
        sector="",
        industry="",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([voo])):
        out = p.search("VOO")
    assert len(out) == 1 and out[0]["type"] == "etf"


def test_sao_etf_still_dropped():
    """B3-listed ETFs collide with the FII ticker shape — keep dropping them."""
    p = YFinanceProvider()
    bova = _quote(
        quoteType="ETF",
        symbol="BOVA11.SA",
        exchange="SAO",
        longname="iShares Ibovespa Fundo de Indice",
        shortname="ISHARES BOVA",
        sector="",
        industry="",
    )
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([bova])):
        out = p.search("BOVA11")
    assert out == []


def test_etf_included_in_us_stocks_scoped_search():
    """Class-scoped usStocks search surfaces equity ETFs alongside stocks."""
    p = YFinanceProvider()
    voo = _quote(quoteType="ETF", symbol="VOO", exchange="PCX",
                 longname="Vanguard S&P 500 ETF", sector="", industry="")
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([voo, _quote()])):
        out = p.search("anything", asset_class="usStocks")
    assert {r["symbol"] for r in out} == {"VOO", "AAPL"}


def test_filters_drop_foreign_exchanges():
    p = YFinanceProvider()
    arg = _quote(symbol="AAPL.BA", exchange="BUE", longname="Apple Argentina CEDEAR")
    chf = _quote(symbol="AAPL.SW", exchange="EBS")
    de = _quote(symbol="AAPY.DE", exchange="GER")
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([arg, chf, de, _quote()])):
        out = p.search("AAPL")
    assert len(out) == 1 and out[0]["symbol"] == "AAPL"


def test_filters_drop_b3_noise_variants():
    p = YFinanceProvider()
    real = _quote(symbol="PETR4.SA", longname="Petrobras", exchange="SAO")
    forward = _quote(symbol="PETR4F.SA", longname="", shortname="PETROBRAS", exchange="SAO")
    option = _quote(symbol="PETR4Q.SA", longname="", shortname="PETROBRAS", exchange="SAO")
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([real, forward, option])):
        out = p.search("PETR4")
    assert [r["symbol"] for r in out] == ["PETR4.SA"]


def test_asset_class_filter_only_returns_matching_class():
    p = YFinanceProvider()
    aapl = _quote()  # common stock
    realty = _quote(symbol="O", exchange="NYQ", longname="Realty Income",
                    sector="Real Estate", industry="REIT—Retail")
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([aapl, realty])):
        only_reits = p.search("anything", asset_class="reits")
    assert [r["symbol"] for r in only_reits] == ["O"]


def test_asset_class_crypto_short_circuits_yfinance():
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search") as mocked:
        out = p.search("BTC", asset_class="crypto")
    assert out == []
    mocked.assert_not_called()


def test_asset_class_renda_fixa_short_circuits_yfinance():
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search") as mocked:
        out = p.search("CDB", asset_class="rendaFixa")
    assert out == []
    mocked.assert_not_called()


def test_br_class_retries_with_sa_suffix():
    """When the user types a bare BR ticker like "BTLG11" (no .SA),
    the provider should retry the query with the suffix appended so
    Yahoo's matcher surfaces the B3 listing."""
    p = YFinanceProvider()
    btlg = _quote(symbol="BTLG11.SA", exchange="SAO", longname="BTLG FII",
                  sector="Real Estate", industry="REIT—Industrial")
    # First query (bare) returns nothing; .SA retry returns the FII.
    results_by_query = {"BTLG11": [], "BTLG11.SA": [btlg]}
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search(results_by_query)):
        out = p.search("BTLG11", asset_class="fiis")
    assert [r["symbol"] for r in out] == ["BTLG11.SA"]


def test_yfinance_failure_returns_empty_not_raises():
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=RuntimeError("boom")):
        out = p.search("AAPL")
    assert out == []


# --- B3SA3 regression tests -------------------------------------------------
# B3SA3 (B3 S.A.) has a digit in position 2 of the company code ("B3SA").
# The old `^[A-Z]{4}\d{1,2}$` pattern would silently drop it from search
# results and misclassify it as a US ticker.

def _b3sa3_quote() -> dict:
    return {
        "quoteType": "EQUITY",
        "exchange": "SAO",
        "symbol": "B3SA3.SA",
        "longname": "B3 S.A. - Brasil, Bolsa, Balcão",
        "shortname": "B3SA3",
        "sector": "Financial Services",
        "industry": "Financial Data & Stock Exchanges",
    }


def test_b3sa3_maps_to_stock_type():
    """B3SA3 must survive the ticker-shape filter and land in the 'stock' bucket."""
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([_b3sa3_quote()])):
        out = p.search("B3SA3")
    assert len(out) == 1
    assert out[0]["symbol"] == "B3SA3.SA"
    assert out[0]["type"] == "stock"


def test_b3sa3_not_classified_as_fund():
    # Ticker ends in "3", not "11" — must NOT be misclassified as a FII.
    # Kills the mutation where the `endswith("11")` branch order is swapped
    # with the catch-all "stock" assignment.
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([_b3sa3_quote()])):
        out = p.search("B3SA3")
    assert out[0]["type"] != "fund"


def test_b3sa3_not_classified_as_bdr():
    # Ticker does not end in a BDR suffix (32/33/34/35/39) — must not be
    # misclassified as a BDR.
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([_b3sa3_quote()])):
        out = p.search("B3SA3")
    assert out[0]["type"] != "bdr"


def test_b3sa3_acoesBR_scoped_search_includes_result():
    """An asset_class="acoesBR" scoped search must include B3SA3 in results."""
    p = YFinanceProvider()
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([_b3sa3_quote()])):
        out = p.search("B3SA3", asset_class="acoesBR")
    assert any(r["symbol"] == "B3SA3.SA" for r in out)


def test_b3sa3_no_longname_is_dropped():
    # SAO tickers without a longname are B3 noise (options/forwards). B3SA3
    # WITH a longname must pass; B3SA3 WITHOUT must be dropped.
    p = YFinanceProvider()
    noise = {**_b3sa3_quote(), "longname": "", "shortname": "B3SA3"}
    with patch("app.providers.yfinance.yf.Search", side_effect=_mock_search([noise])):
        out = p.search("B3SA3")
    assert out == []
