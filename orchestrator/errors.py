"""Exceptions shared by more than one component."""


class StaleEpoch(Exception):
    """A write or tool call carried an epoch that is no longer the session's current epoch.

    The caller is a "zombie": the session was reassigned after the caller was
    given its epoch. The caller must stop; it must not retry with a new epoch.
    """


class UnknownSession(Exception):
    """The session id does not exist. This is a bug in the caller, not a fencing event."""
