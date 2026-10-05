# story-gate demo

A tiny shop app used to show [story-gate](https://github.com/SathiaAI/story-gate) working on real pull requests.

- `price.py` works out an order total: `python price.py 3 2.00` prints `6.00`.
- Tests: `python -m unittest discover -s tests -t .`

Every change here goes through story-gate: the AI writes the story and its tests first, shows each goal working, an independent judge scores the work, and a person approves on GitHub. Look at the pull requests to see it pass and block.
