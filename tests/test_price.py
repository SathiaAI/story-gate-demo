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


    def test_totals_below_ten_items_are_unchanged(self):
        for qty in range(1, 10):  # regression: the discount must not touch smaller orders
            self.assertAlmostEqual(total(qty, 2.50), qty * 2.50)


class TestBulkDiscount(unittest.TestCase):
    def test_big_order_gets_ten_percent_off(self):
        self.assertAlmostEqual(total(12, 4.00), 43.20)

    def test_exactly_ten_items_get_the_discount(self):
        self.assertAlmostEqual(total(10, 4.00), 36.00)

    def test_eleven_items_get_the_discount(self):
        self.assertAlmostEqual(total(11, 4.00), 39.60)

    def test_small_order_pays_full_price(self):
        self.assertAlmostEqual(total(9, 4.00), 36.00)


if __name__ == "__main__":
    unittest.main()
