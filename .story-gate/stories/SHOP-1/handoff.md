# Handoff: SHOP-1

## What changed
`total()` in price.py now takes 10% off orders of 10 or more items (`qty >= 10`, so exactly 10 counts).

## Interfaces and contracts
`total(qty, unit)` keeps its signature. It returns a number; the command line prints it with 2 decimals.

## How to verify
Run `python price.py 10 4.00` (expect 36.00) and `python price.py 9 4.00` (expect 36.00, full price).

## Known limits
No discount codes and no other discount levels.

## Downstream consumers
None — nothing else in this repository calls `total()`.

## Release and rollback
Ships with the merge. To roll back, revert this pull request.

## Drift decisions
None — the build follows the story.
