"""Exceptions shared by more than one component."""


class StaleEpoch(Exception):
    """A write or tool call carried an epoch that is no longer the session's current epoch.

    The caller is a "zombie": the session was reassigned after the caller was
    given its epoch. The caller must stop; it must not retry with a new epoch.
    """


class UnknownSession(Exception):
    """The session id does not exist. This is a bug in the caller, not a fencing event."""


class SessionClosed(Exception):
    """The session is in a terminal state (done, cancelled, expired, stopped) and cannot be assigned."""


class KeyConflict(Exception):
    """An idempotency key was reused for a different tool or different arguments. A caller bug."""


class QuotaExceeded(Exception):
    """Admission refused: the tenant already has its maximum number of live sessions."""


class BudgetExceeded(Exception):
    """Admission refused: the rollup job has flagged the tenant as over budget."""


class UnknownAgent(Exception):
    """No agent definition with this id."""
