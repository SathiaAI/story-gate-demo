---
id: SHOP-1
title: Bulk discount on orders
feature: none
source: repo:README.md (demo story, written for this public example)
depends_on: []
consumers: []
---
## Plain summary
Shoppers who buy 10 or more items get 10% off. The order total shows the discount. Smaller orders pay full price. We know it works when the totals for 9, 10 and 12 items are right.

## Story (word for word from the source)
As a shopper, I get 10% off when I buy 10 or more items, so that buying in bulk is worth it.
Out of scope: discount codes, other discount levels.
AC-1: The total is the quantity times the unit price.
AC-2: Orders of 10 or more items get 10% off the total.
