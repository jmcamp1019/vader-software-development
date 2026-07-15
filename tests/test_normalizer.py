import unittest

import _path  # noqa: F401

from pelositracker.normalizer import (
    clean_ticker,
    normalize_house_record,
    normalize_senate_filing,
    normalize_senate_record,
    normalize_type,
    parse_date,
)

SENATE_FILING = {
    "first_name": "Ima",
    "last_name": "Fixture (Test)",
    "ptr_link": "https://efdsearch.senate.gov/search/view/ptr/TEST-2001/",
    "date_recieved": "05/14/2026",
    "transactions": [
        {
            "transaction_date": "04/06/2026",
            "owner": "Spouse",
            "ticker": "TSLA",
            "asset_description": "Tesla Inc - TEST DATA",
            "type": "Sale (Full)",
            "amount": "$100,001 - $250,000",
        },
        {
            "transaction_date": "04/08/2026",
            "owner": "Self",
            "ticker": "MSFT",
            "asset_description": "Microsoft Corporation - TEST DATA",
            "type": "not_a_real_type",
            "amount": "$1,001 - $15,000",
        },
    ],
}

HOUSE_RECORD = {
    "disclosure_date": "05/12/2026",
    "transaction_date": "2026-04-03",
    "owner": "self",
    "ticker": "NVDA",
    "asset_description": "NVIDIA Corp - TEST DATA",
    "type": "sale_partial",
    "amount": "$1,001 - $15,000",
    "representative": "Hon. Testa Fixture",
    "ptr_link": "https://disclosures-clerk.house.gov/ptr/TEST-0002",
}

SENATE_RECORD = {
    "transaction_date": "04/06/2026",
    "disclosure_date": "05/14/2026",
    "owner": "Spouse",
    "ticker": "--",
    "asset_description": "Tesla Inc - TEST DATA",
    "type": "Sale (Full)",
    "amount": "$100,001 - $250,000",
    "senator": "Fixture, Ima (Test)",
    "ptr_link": "https://efdsearch.senate.gov/search/view/ptr/TEST-1001/",
}


class NormalizerTests(unittest.TestCase):
    def test_house_record_normalizes(self) -> None:
        trade = normalize_house_record(HOUSE_RECORD)
        self.assertEqual(trade.politician_name, "Testa Fixture")  # "Hon. " stripped
        self.assertEqual(trade.chamber, "house")
        self.assertEqual(trade.transaction_type, "sell")  # sale_partial -> sell
        self.assertEqual(trade.transaction_date, "2026-04-03")
        self.assertEqual(trade.disclosure_date, "2026-05-12")
        self.assertEqual(trade.amount_min_cents, 100_100)
        self.assertEqual(trade.amount_max_cents, 1_500_000)
        self.assertTrue(trade.source_url.startswith("https://"))

    def test_senate_record_normalizes_unknown_ticker(self) -> None:
        trade = normalize_senate_record(SENATE_RECORD)
        self.assertEqual(trade.chamber, "senate")
        self.assertIsNone(trade.ticker)  # "--" means unknown
        self.assertEqual(trade.transaction_type, "sell")

    def test_hash_is_deterministic_and_distinct(self) -> None:
        a = normalize_house_record(HOUSE_RECORD)
        b = normalize_house_record(dict(HOUSE_RECORD))
        self.assertEqual(a.ingest_hash, b.ingest_hash)
        modified = dict(HOUSE_RECORD, transaction_date="2026-04-04")
        c = normalize_house_record(modified)
        self.assertNotEqual(a.ingest_hash, c.ingest_hash)

    def test_bad_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_date("not-a-date")

    def test_unknown_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_type("gifted")

    def test_ticker_cleaning(self) -> None:
        self.assertIsNone(clean_ticker("--"))
        self.assertIsNone(clean_ticker(""))
        self.assertEqual(clean_ticker(" msft "), "MSFT")

    def test_missing_source_url_raises(self) -> None:
        record = dict(HOUSE_RECORD, ptr_link="")
        with self.assertRaises(ValueError):
            normalize_house_record(record)

    def test_senate_filing_flattens_transactions_and_skips_malformed(self) -> None:
        trades = normalize_senate_filing(SENATE_FILING)
        self.assertEqual(len(trades), 1)  # the "not_a_real_type" row is skipped
        trade = trades[0]
        self.assertEqual(trade.politician_name, "Ima Fixture (Test)")
        self.assertEqual(trade.chamber, "senate")
        self.assertEqual(trade.disclosure_date, "2026-05-14")  # from date_recieved
        self.assertEqual(trade.source_url, SENATE_FILING["ptr_link"])

    def test_senate_filing_missing_date_recieved_raises(self) -> None:
        filing = dict(SENATE_FILING, date_recieved="")
        with self.assertRaises(ValueError):
            normalize_senate_filing(filing)

    def test_senate_filing_missing_ptr_link_raises(self) -> None:
        filing = dict(SENATE_FILING, ptr_link="")
        with self.assertRaises(ValueError):
            normalize_senate_filing(filing)


if __name__ == "__main__":
    unittest.main()
