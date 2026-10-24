"""Shared refusal type; importing the SDK never imports a native CLI adapter."""

class NativeRequestError(RuntimeError):
    """The upstream answered with a protocol refusal; never replay it."""
