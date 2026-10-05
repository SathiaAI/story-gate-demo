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


class TestBulkDiscount(unittest.TestCase):
    def test_big_order_gets_ten_percent_off(self):
        self.assertAlmostEqual(total(12, 4.00), 43.20)

    def test_exactly_ten_items_get_the_discount(self):
        self.assertAlmostEqual(total(10, 4.00), 36.00)

    def test_small_order_pays_full_price(self):
        self.assertAlmostEqual(total(9, 4.00), 36.00)


if __name__ == "__main__":
    unittest.main()
