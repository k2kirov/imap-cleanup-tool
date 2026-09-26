"""A small multi-folder IMAP fake for auto-sort tests."""

from __future__ import annotations

import imaplib
from datetime import datetime, timezone

DEFAULT_RECEIVED = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


class FakeMailbox:
    def __init__(self, folders=("INBOX",), *, capabilities=("IMAP4REV1", "MOVE"),
                 delimiter=".", sent="Sent"):
        self.capabilities = capabilities
        self.delimiter = delimiter
        self.sent = sent
        self.folders = {name: {} for name in folders}
        if sent:
            self.folders.setdefault(sent, {})
        self.validity = {name: "7" for name in self.folders}
        self.next_uid = {name: 1 for name in self.folders}
        self.selected = None
        self.readonly = True
        self.calls = []
        self.gmail = {}   # Gmail category name -> set of INBOX UIDs

    def add(self, folder, *, sender, subject, message_id="", to="me@example.com",
            cc="", extra="", flags=(), received=None) -> str:
        uid = str(self.next_uid[folder])
        self.next_uid[folder] += 1
        header = (f"From: <{sender}>\r\nTo: {to}\r\nSubject: {subject}\r\n"
                  "Date: Thu, 24 Sep 2026 10:00:00 +0000\r\n")
        if cc:
            header += f"Cc: {cc}\r\n"
        if message_id:
            header += f"Message-ID: {message_id}\r\n"
        header += extra
        self.folders[folder][uid] = {"header": header + "\r\n", "flags": tuple(flags),
                                     "received": received or DEFAULT_RECEIVED}
        return uid

    def list(self, *args):
        lines = []
        for name in self.folders:
            attrs = "\\HasNoChildren" + (" \\Sent" if name == self.sent else "")
            lines.append(f'({attrs}) "{self.delimiter}" "{name}"'.encode())
        return "OK", lines

    def create(self, name):
        name = name.strip('"')
        self.calls.append(("CREATE", name))
        self.folders.setdefault(name, {})
        self.validity.setdefault(name, "7")
        self.next_uid.setdefault(name, 1)
        return "OK", [b"created"]

    def subscribe(self, name):
        return "OK", [b""]

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.folders[name])).encode()]

    def response(self, code):
        return code, [self.validity[self.selected].encode()]

    def uid(self, command, *args):
        self.calls.append((command, self.selected, *args))
        box = self.folders[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(self._search(box, args)).encode()]
        if command == "FETCH":
            parts = []
            for raw in args[0].split(b","):
                msg = box.get(raw.decode())
                if msg is None:
                    continue
                header = msg["header"].encode()
                internal = imaplib.Time2Internaldate(msg["received"].timestamp())
                meta = (f"1 (UID {raw.decode()} FLAGS ({' '.join(msg['flags'])}) "
                        f"INTERNALDATE {internal} BODY[HEADER] {{{len(header)}}}").encode()
                parts.append((meta, header))
                parts.append(b")")
            return "OK", parts
        if command == "MOVE":
            if self.readonly:
                raise AssertionError("MOVE on a read-only folder")
            dest = args[1].strip('"')
            msg = box.pop(args[0].decode())
            new_uid = str(self.next_uid[dest])
            self.next_uid[dest] += 1
            self.folders[dest][new_uid] = msg
            return "OK", [b"moved"]
        raise AssertionError(command)

    def _search(self, box, args):
        uids = sorted(box, key=int)
        args = [a for a in args if a is not None]
        if args == ["ALL"]:
            return uids
        if args[0] == "UID":
            low = int(args[1].split(":")[0])
            # Like real servers, "n:*" returns the last message when none is >= n.
            return [u for u in uids if int(u) >= low] or uids[-1:]
        if args[0] == "SINCE":
            day = datetime.strptime(args[1], "%d-%b-%Y").replace(tzinfo=timezone.utc)
            return [u for u in uids if box[u]["received"] >= day]
        if args[:2] == ["HEADER", "Message-ID"]:
            want = args[2].strip('"')
            return [u for u in uids if f"Message-ID: {want}\r\n" in box[u]["header"]]
        if args[0] == "X-GM-RAW":
            category = args[1].strip('"').split(":", 1)[1]
            return sorted(self.gmail.get(category, set()) & set(uids), key=int)
        raise AssertionError(args)
