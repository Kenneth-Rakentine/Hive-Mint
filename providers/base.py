"""Shared provider types."""

from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(RuntimeError):
    """The provider answered, but not usefully (bad status, unparseable or invalid output).

    Token counts are kept when the provider reported them, so a failed call is still logged in full."""

    def __init__(self, message: str, *, prompt_tokens: int = 0, completion_tokens: int = 0):
        super().__init__(message)
        self.prompt_tokens, self.completion_tokens = prompt_tokens, completion_tokens


class ProviderUnavailable(ProviderError):
    """The provider could not be reached at all (service off, refused, timed out)."""


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    data: object = field(default=None)  # parsed structured output, when a response model was given
