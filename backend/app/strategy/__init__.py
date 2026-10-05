"""Strategy package — shared Strategy Engine (ONE code path for BOTH backtest AND live).

Architecture (NON-NEGOTIABLE PER SPEC #1 RULE):
  on_bar(context) -> orders is ALWAYS the only code path.
  Backtest driver loops over historical bars and calls on_bar for each.
  Live engine subscribes to feed bar-close events and calls on_bar for each.
  NEVER write separate backtest logic separately; NEVER write live logic separately.
  Any divergence between the two is an ENGINE BUG that loses money → parity check catches it.
"""

# Import strategies submodule so @register_strategy decorators fire and populate registry.
from app.strategy import strategies as _strategies  # noqa: F401
from app.strategy import building_blocks as building_blocks  # noqa: F401

__all__ = ["strategies", "building_blocks"]
