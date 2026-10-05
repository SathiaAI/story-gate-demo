"""Order totals for a small online shop (the story-gate demo app)."""
import sys


def total(qty, unit):
    """Order total: quantity times unit price."""
    if qty < 0 or unit < 0:
        raise ValueError("quantity and price can't be negative")
    return qty * unit


if __name__ == "__main__":
    print("%.2f" % total(int(sys.argv[1]), float(sys.argv[2])))
