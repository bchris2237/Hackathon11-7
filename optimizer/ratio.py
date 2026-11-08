"""Exact comparison of effort/baseline shares (the frozen contract's second tie-break).

The contract says: after the least total effort, prefer the allocation whose WORST individual effort/baseline ratio is smallest,
compared with integer cross multiplication and with a zero baseline handled explicitly. `RatioRank` does exactly that:
`a/b < c/d` is decided as `a*d < c*b` on integers. No division, no floating point, no rounding.

A zero baseline means the business needs nothing of its own done, so any effort it takes on is an unbounded share of what it
"saved". It therefore ranks above every finite share. Two zero-baseline shares tie, so the next rule (provider ids) decides.
"""

from __future__ import annotations

from functools import total_ordering


@total_ordering
class RatioRank:
    """effort/baseline with exact ordering. `baseline == 0` is the largest possible value."""

    __slots__ = ("effort", "baseline")

    def __init__(self, effort: int, baseline: int) -> None:
        if effort < 0 or baseline < 0:
            raise ValueError("effort and baseline must be non-negative integers")
        self.effort = effort
        self.baseline = baseline

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RatioRank):
            return NotImplemented
        if self.baseline == 0 or other.baseline == 0:
            return self.baseline == other.baseline
        return self.effort * other.baseline == other.effort * self.baseline

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RatioRank):
            return NotImplemented
        if self.baseline == 0:
            return False  # infinity is never smaller than anything
        if other.baseline == 0:
            return True  # a finite share is smaller than infinity
        return self.effort * other.baseline < other.effort * self.baseline

    def __hash__(self) -> int:
        return hash(("inf",)) if self.baseline == 0 else hash((self.effort, self.baseline))

    def __repr__(self) -> str:
        return f"RatioRank({self.effort}/{self.baseline})"


def ratio_rank(effort: int, baseline: int) -> RatioRank:
    return RatioRank(effort, baseline)
