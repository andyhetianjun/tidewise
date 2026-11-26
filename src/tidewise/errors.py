"""Error types. Messages are written for the model (or person) reading them."""


class TidewiseError(Exception):
    """A problem the caller can fix: bad input, unknown station, no data."""


class TransientError(Exception):
    """A failure that may go away if the same request is tried again (network, rate limit)."""
