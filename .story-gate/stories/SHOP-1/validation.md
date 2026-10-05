# Validation: SHOP-1
The fixed version of the staged demo. Pull request #5 shows story-gate blocking the bug; this one shows the same story passing after the fix.

## Result
Done. Orders of 10 or more items get 10% off, including exactly 10. Smaller orders pay full price.

## Acceptance criteria
- AC-1: met. Shown by the scenario "3 items cost 6.00" and the unit tests.
- AC-2: met. Shown by the scenarios "exactly 10 items get the discount", "12 items get the discount" and "9 items pay full price", and the unit tests.

## Scenarios run
Four runs of `price.py`: 3, 9, 10 and 12 items. All four pass.

## Bugs found and fixed
Found by story-gate: `price.py` checked `qty > 10`, so exactly 10 items paid full price (40.00, not 36.00). Fixed to `qty >= 10`. Added the unit test for exactly 10 items, then ran the scenarios again.

## Lessons learnt
Test the exact boundary a story names ("10 or more" means 10 counts).

## Known limits
No discount codes and no other discount levels (out of scope).

## Demo
Run `python price.py 10 4.00`. It prints 36.00.
