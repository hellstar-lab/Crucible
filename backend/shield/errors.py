"""Shield exceptions. A malformed upload must raise one of these, never crash."""


class ShieldError(Exception):
    """Base class for every error Shield raises on bad input."""


class ShieldEncryptedError(ShieldError):
    """The PDF needs a password to open."""


class ShieldCorruptError(ShieldError):
    """The file is not a readable PDF (truncated, malformed, or not a PDF)."""
