"""Order totals for a small online shop (the story-gate demo app)."""
import sys


def total(qty, unit):
    """Order total: quantity times unit price. Orders of 10 or more items get 10% off."""
    if qty < 0 or unit < 0:
        raise ValueError("quantity and price can't be negative")
    t = qty * unit
    if qty >= 10:
        t = t * 0.9
    return t


if __name__ == "__main__":
    print("%.2f" % total(int(sys.argv[1]), float(sys.argv[2])))
