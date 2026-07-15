import unittest

import _path  # noqa: F401

from pelositracker.amounts import parse_amount_range


class AmountRangeTests(unittest.TestCase):
    def test_standard_range(self) -> None:
        self.assertEqual(parse_amount_range("$1,001 - $15,000"), (100_100, 1_500_000))

    def test_large_range_with_commas(self) -> None:
        self.assertEqual(
            parse_amount_range("$1,000,001 - $5,000,000"), (100_000_100, 500_000_000)
        )

    def test_open_ended_range_has_no_max(self) -> None:
        low, high = parse_amount_range("$50,000,000 +")
        self.assertEqual(low, 5_000_000_000)
        self.assertIsNone(high)

    def test_never_collapses_to_point_value(self) -> None:
        low, high = parse_amount_range("$15,001 - $50,000")
        self.assertNotEqual(low, high)

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_amount_range("undisclosed")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_amount_range("")
        with self.assertRaises(ValueError):
            parse_amount_range(None)


if __name__ == "__main__":
    unittest.main()
