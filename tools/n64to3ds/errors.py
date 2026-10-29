"""User-facing error types for n64to3ds.

The CLI catches these and prints a single clear diagnostic instead of a Python
traceback.  Unexpected exceptions are still reported with a traceback in
verbose mode so that build failures remain diagnosable.
"""

from __future__ import annotations


class N64To3DSError(Exception):
    """Base class for expected, actionable failures."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def format(self) -> str:
        if self.hint:
            return f"{self.message}\n\nHint: {self.hint}"
        return self.message


class RomError(N64To3DSError):
    """The input file is not a usable N64 ROM."""


class ToolchainError(N64To3DSError):
    """A required host tool is missing or unusable."""


class BuildError(N64To3DSError):
    """A build step failed."""


class AnalyzerError(N64To3DSError):
    """Static analysis failed in a way that cannot safely fall back."""


class VerificationError(N64To3DSError):
    """The produced artifact failed verification."""
