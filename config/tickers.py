
VALIDATION_UNIVERSE: list[str] = [
    # Tech (17)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "ORCL",
    "AMD", "QCOM", "TXN", "CRM", "ADBE", "NFLX", "NOW", "INTC",
    # Healthcare (9)
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
    # Finance (8)
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "C",
    # Consumer (8)
    "WMT", "COST", "HD", "MCD", "KO", "PEP", "NKE", "DIS",
    # Energy (2)
    "XOM", "CVX",
    # Industrial (4)
    "CAT", "UNP", "RTX", "HON",
    # Other (2)
    "GE", "MMM",
]

SNAPSHOT_DATES: list[str] = [
    "2018-12-31", "2019-12-31", "2020-12-31", "2021-12-31",
    "2022-12-31", "2023-12-31", "2024-12-31",
]
FORWARD_MONTHS: int = 12
