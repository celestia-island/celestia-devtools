"""celestia-devtools — shared build/devtool scripts for celestia-island.

Decouples devtooling from individual crates.  Consumed by entelecheia,
shittim-chest, evernight, and other repos in the celestia-island ecosystem.
"""

__version__ = "0.14.2"
# NOTE: this literal is the single source of truth — pyproject declares
# dynamic = ["version"] and hatchling derives the dist metadata from here.
# tests/test_version_single_source.py fails if anyone re-adds a static version.
