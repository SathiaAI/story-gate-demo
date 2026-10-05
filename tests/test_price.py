import unittest

from price import total


class TestTotal(unittest.TestCase):
    def test_quantity_times_price(self):
        self.assertAlmostEqual(total(3, 2.00), 6.00)

    def test_zero_items(self):
        self.assertEqual(total(0, 2.00), 0)

    def test_negative_is_refused(self):
        with self.assertRaises(ValueError):
            total(-1, 2.00)


if __name__ == "__main__":
    unittest.main()
