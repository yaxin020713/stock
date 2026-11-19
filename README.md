# stock scanners

Daily chart scanners. Education and tooling, not financial advice. A hit is a chart that matched a rule, not a buy.

- `tw_scan.py` Taiwan (TWSE + TPEx): `python3 tw_scan.py daily` (run `universe` first, or it builds it itself). Writes `tw/watchlist.json`.
- `us_scan.py` US: `python3 us_scan.py universe` then `python3 us_scan.py daily`. Writes `trader/watchlist.json`.

Standard library only, Python 3. Data: Yahoo (free, unofficial, may lag), TWSE / TPEx OpenAPI, Nasdaq Trader symbol directories.
