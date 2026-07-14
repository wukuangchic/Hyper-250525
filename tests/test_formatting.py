import unittest
from decimal import Decimal

from simple_hyper.formatting import decimal_to_plain


class DecimalToPlainTests(unittest.TestCase):
    def test_accepts_decimal(self):
        self.assertEqual(decimal_to_plain(Decimal("123.4500")), "123.45")

    def test_accepts_numeric_string_from_persisted_state(self):
        self.assertEqual(decimal_to_plain("123.4500"), "123.45")


if __name__ == "__main__":
    unittest.main()
