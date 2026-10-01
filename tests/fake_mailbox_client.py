from __future__ import annotations

import imaplib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

_SECTION_REQUEST = re.compile(
    r"^(?P<selector>BODY\.PEEK\[[^]]+\])(?:<(?P<offset>\d+)\.(?P<count>\d+)>)?$"
)


@dataclass
class FakeMailboxClient:
    """Deterministic, UID-only mailbox double for extension operation tests."""

    uidvalidity: int = 42
    uids: Iterable[int] = field(default_factory=lambda: range(1, 4))
    attachments: set[int] = field(default_factory=set)
    bodystructures: dict[int, object] = field(default_factory=dict)
    message_sections: dict[int, dict[str, bytes]] = field(default_factory=dict)
    capability_values: frozenset[bytes] = field(default_factory=frozenset)
    folders: list[tuple[tuple[bytes, ...], bytes | None, str]] = field(
        default_factory=lambda: [((b"\\HasNoChildren",), b"/", "INBOX")]
    )
    highest_modseq: int | None = None
    selected_folders: list[tuple[str, bool]] = field(default_factory=list)
    searches: list[tuple[Sequence[object] | str, str | None]] = field(default_factory=list)
    fetches: list[tuple[list[int], tuple[str, ...]]] = field(default_factory=list)
    mutation_calls: list[str] = field(default_factory=list)
    mailbox_uids: dict[str, set[int]] = field(default_factory=dict)
    flags_by_uid: dict[int, set[str]] = field(default_factory=dict)
    mutation_details: list[tuple[Any, ...]] = field(default_factory=list)
    mutation_failures: dict[tuple[str, int], Exception] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._uids = sorted(set(self.uids))
        if not self.mailbox_uids:
            self.mailbox_uids = {name: set(self._uids) for _flags, _delimiter, name in self.folders}
            self.mailbox_uids.setdefault("INBOX", set(self._uids))
        else:
            self.mailbox_uids = {
                name: set(mailbox_uids) for name, mailbox_uids in self.mailbox_uids.items()
            }
        for uid in self._uids:
            self.flags_by_uid.setdefault(uid, {"\\Seen"} if uid % 2 == 0 else set())
        self._selected_folder: str | None = None

    def capabilities(self) -> frozenset[bytes]:
        return self.capability_values

    def list_folders(
        self, directory: str = "", pattern: str = "*"
    ) -> list[tuple[tuple[bytes, ...], bytes | None, str]]:
        assert directory == ""
        assert pattern == "*"
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        self.selected_folders.append((folder, readonly))
        if folder not in {name for _flags, _delimiter, name in self.folders}:
            raise imaplib.IMAP4.error("mailbox not found")
        self._selected_folder = folder
        response: dict[bytes, object] = {b"UIDVALIDITY": self.uidvalidity}
        if self.highest_modseq is not None:
            response[b"HIGHESTMODSEQ"] = self.highest_modseq
        return response

    def search(
        self, criteria: Sequence[object] | str = "ALL", charset: str | None = None
    ) -> list[int]:
        self.searches.append((criteria, charset))
        if self._selected_folder is None:
            return self._uids
        return sorted(self.mailbox_uids.get(self._selected_folder, set()))

    def fetch(
        self,
        messages: int | Iterable[int],
        data: Sequence[str] | str,
        modifiers: Sequence[str] | None = None,
    ) -> dict[int, dict[bytes, object]]:
        assert modifiers is None
        message_uids = [messages] if isinstance(messages, int) else list(messages)
        fields = (data,) if isinstance(data, str) else tuple(data)
        self.fetches.append((message_uids, fields))
        available_uids = (
            set(self._uids)
            if self._selected_folder is None
            else self.mailbox_uids.get(self._selected_folder, set())
        )
        result = {
            uid: {
                b"UID": uid,
                b"RFC822.SIZE": uid * 10,
                b"FLAGS": tuple(flag.encode("ascii") for flag in sorted(self.flags_by_uid[uid])),
                b"BODYSTRUCTURE": self.bodystructures.get(
                    uid,
                    (
                        b"APPLICATION",
                        b"OCTET-STREAM",
                        None,
                        None,
                        None,
                        b"BASE64",
                        1,
                        None,
                        (b"ATTACHMENT", (b"FILENAME", b"attachment.bin")),
                    )
                    if uid in self.attachments
                    else (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1, None, None, None),
                ),
            }
            for uid in message_uids
            if uid in available_uids
        }
        for uid, values in result.items():
            sections = self.message_sections.get(uid, {})
            for field_name in fields:
                match = _SECTION_REQUEST.fullmatch(field_name)
                if match is None:
                    continue
                response_selector = match.group("selector").replace("BODY.PEEK[", "BODY[")
                if response_selector not in sections:
                    continue
                section = sections[response_selector]
                if match.group("offset") is not None:
                    offset = int(match.group("offset"))
                    count = int(match.group("count"))
                    section = section[offset : offset + count]
                values[response_selector.encode("ascii")] = section
        return result

    def add_flags(
        self,
        messages: int | Iterable[int],
        flags: Sequence[str],
        silent: bool = False,
    ) -> dict[int, tuple[bytes, ...]] | None:
        uid = self._one_uid(messages)
        self.mutation_calls.append("STORE")
        self.mutation_details.append(("STORE_ADD", uid, tuple(flags)))
        self._raise_mutation_failure("STORE_ADD", uid)
        self.flags_by_uid.setdefault(uid, set()).update(flags)
        if silent:
            return None
        return {uid: tuple(flag.encode("ascii") for flag in sorted(self.flags_by_uid[uid]))}

    def remove_flags(
        self,
        messages: int | Iterable[int],
        flags: Sequence[str],
        silent: bool = False,
    ) -> dict[int, tuple[bytes, ...]] | None:
        uid = self._one_uid(messages)
        self.mutation_calls.append("STORE")
        self.mutation_details.append(("STORE_REMOVE", uid, tuple(flags)))
        self._raise_mutation_failure("STORE_REMOVE", uid)
        self.flags_by_uid.setdefault(uid, set()).difference_update(flags)
        if silent:
            return None
        return {uid: tuple(flag.encode("ascii") for flag in sorted(self.flags_by_uid[uid]))}

    def move(self, messages: int | Iterable[int], folder: str) -> bytes:
        uid = self._one_uid(messages)
        self.mutation_calls.append("MOVE")
        self.mutation_details.append(("MOVE", uid, folder))
        self._raise_mutation_failure("MOVE", uid)
        source = self._selected_mailbox()
        if folder not in self.mailbox_uids or uid not in self.mailbox_uids[source]:
            raise imaplib.IMAP4.error("move failed")
        self.mailbox_uids[source].remove(uid)
        self.mailbox_uids[folder].add(uid)
        return b""

    def copy(self, messages: int | Iterable[int], folder: str) -> bytes:
        uid = self._one_uid(messages)
        self.mutation_calls.append("COPY")
        self.mutation_details.append(("COPY", uid, folder))
        self._raise_mutation_failure("COPY", uid)
        source = self._selected_mailbox()
        if folder not in self.mailbox_uids or uid not in self.mailbox_uids[source]:
            raise imaplib.IMAP4.error("copy failed")
        self.mailbox_uids[folder].add(uid)
        self.flags_by_uid.setdefault(uid, set())
        return b""

    def uid_expunge(self, messages: int | Iterable[int]) -> bytes:
        uid = self._one_uid(messages)
        self.mutation_calls.append("UID EXPUNGE")
        self.mutation_details.append(("UID_EXPUNGE", uid))
        self._raise_mutation_failure("UID_EXPUNGE", uid)
        source = self._selected_mailbox()
        if "\\Deleted" in self.flags_by_uid.get(uid, set()):
            self.mailbox_uids[source].discard(uid)
            self.flags_by_uid[uid].discard("\\Deleted")
        return b""

    def _one_uid(self, messages: int | Iterable[int]) -> int:
        values = [messages] if isinstance(messages, int) else list(messages)
        assert len(values) == 1
        return values[0]

    def _selected_mailbox(self) -> str:
        assert self._selected_folder is not None
        return self._selected_folder

    def _raise_mutation_failure(self, operation: str, uid: int) -> None:
        failure = self.mutation_failures.get((operation, uid))
        if failure is not None:
            raise failure
