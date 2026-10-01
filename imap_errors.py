from __future__ import annotations


class ImapExtensionError(RuntimeError):
    """A stable, safe error surfaced by the IMAP extension."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        external_effect_status: str | None = None,
        definitely_no_external_effect: bool = True,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.external_effect_status = external_effect_status
        self.definitely_no_external_effect = definitely_no_external_effect
