# Validation: SHOP-1
Staged demo: this is the kind of "done" an AI often writes. The bug is real and left in on purpose, so you can see story-gate block the pull request.

## Result
Done. Orders of 10 or more items now get 10% off, and smaller orders pay full price. The unit tests pass.

## Acceptance criteria
- AC-1: met. Shown by the scenario "3 items cost 6.00" and the unit tests.
- AC-2: met. Shown by the scenario "12 items get the discount" and the unit tests.

## Scenarios run
Four runs of `price.py`: 3 items, 9 items, 12 items and exactly 10 items.

## Bugs found and fixed
None found.

## Lessons learnt
None.

## Known limits
No discount codes and no other discount levels (out of scope).

## Demo
Run `python price.py 10 4.00`. It should print 36.00.
