"""Shared safe errors, independent of how the helper entrypoint is launched."""

from ..runtime import safe_summary
from ..validation import bounded_text, safe_code


class HelperActionError(RuntimeError):
    """A deliberate safe failure returned to the controller."""

    def __init__(self, code: str, message: str) -> None:
        self.code = safe_code(code)
        self.message = bounded_text(
            safe_summary(message), field="helper error message", maximum=1_024
        )
        super().__init__(self.message)
