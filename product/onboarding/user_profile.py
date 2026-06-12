"""User profile: watchlist, risk preference label, and onboarding state.

Stores user preferences that affect UI framing only.
Risk preference does NOT affect the signal, thresholds, or exit rule.
The signal is identical for all users — only the copy framing differs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Literal, Optional

RiskLabel = Literal["conservative", "balanced", "growth"]


@dataclass
class UserProfile:
    """Persistent user preferences for the stock screener app.

    Attributes:
        user_id:          Unique user identifier.
        watchlist:        List of tickers the user wants alerts for.
                          If empty, alerts are generated for the full
                          50-ticker universe.
        risk_label:       User's self-selected risk label.
                          Affects alert copy framing only — NOT the signal.
        onboarding_done:  True once the user has read the magnitude-edge
                          onboarding and accepted the 252d hold expectation.
        created_date:     Account creation date.
    """

    user_id: str
    watchlist: List[str] = field(default_factory=list)
    risk_label: RiskLabel = "balanced"
    onboarding_done: bool = False
    created_date: Optional[date] = None


class UserProfileStore:
    """Persist and retrieve UserProfile objects.

    Implementation is storage-agnostic — subclass to back by a database,
    JSON file, or in-memory dict for testing.
    """

    def get(self, user_id: str) -> Optional[UserProfile]:
        """Retrieve a user profile by ID.

        Args:
            user_id: Unique user identifier.

        Returns:
            UserProfile or None if user not found.
        """
        # TODO: implement
        raise NotImplementedError

    def save(self, profile: UserProfile) -> None:
        """Persist (create or update) a user profile.

        Args:
            profile: UserProfile to save.
        """
        # TODO: implement
        raise NotImplementedError

    def add_to_watchlist(self, user_id: str, ticker: str) -> None:
        """Add a ticker to the user's watchlist (idempotent).

        Args:
            user_id: Unique user identifier.
            ticker:  Ticker symbol to add (case-insensitive).
        """
        # TODO: implement — normalise ticker to uppercase, avoid duplicates
        raise NotImplementedError

    def remove_from_watchlist(self, user_id: str, ticker: str) -> None:
        """Remove a ticker from the user's watchlist.

        Args:
            user_id: Unique user identifier.
            ticker:  Ticker symbol to remove.
        """
        # TODO: implement
        raise NotImplementedError

    def mark_onboarding_done(self, user_id: str) -> None:
        """Record that user has completed magnitude-edge onboarding.

        Args:
            user_id: Unique user identifier.
        """
        # TODO: implement
        raise NotImplementedError
