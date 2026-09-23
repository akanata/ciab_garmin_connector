"""One warning per distinct token, ever.

Mapping a vendor's vocabulary onto the spec's always has an unmapped case, and
that case is per *row*: a single night is hundreds of sleep events, so warning
each time would bury the signal in its own noise. Warning once per distinct token
makes a newly added vendor value visible exactly once, which is what the operator
needs to act on it.

Generic rather than GarminDB's, because the shape belongs to any provider
translating a vendor enum -- an aggregator's activity levels would need the same
thing. The logger is injected so the warning names the module that owns the
vocabulary rather than this one.
"""

from __future__ import annotations

import logging
import threading


class WarnOnce:
    """A set of tokens already warned about, and the warning to emit for a new one."""

    def __init__(self, logger: logging.Logger, message: str) -> None:
        """``message`` is a %-style template taking exactly one argument: the token."""
        self._logger = logger
        self._message = message
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def __call__(self, token: str) -> bool:
        """Warn about ``token`` unless it has been warned about before.

        Returns whether it warned. A check-then-add without the lock would warn
        once per racing thread, and sleep sessions really are built on worker
        threads that can meet the same unmapped token at the same instant.
        """
        with self._lock:
            if token in self._seen:
                return False
            self._seen.add(token)
        # Deliberately outside the lock. A handler can be slow -- a file, a
        # socket, a remote collector -- and holding the lock across it would
        # serialize every other reader behind logging.
        self._logger.warning(self._message, token)
        return True

    def reset(self) -> None:
        """Forget every token seen so far. A test seam.

        The registry is process-global, which is the point of it: a test that did
        not reset would pass or fail depending on what ran before it.
        """
        with self._lock:
            self._seen.clear()
