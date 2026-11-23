from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory
import unittest

from claudex.models import PEER_MESSAGE, PeerMessage, Provider, Sender
from claudex.mailbox import Mailbox, MessagePage


MESSAGE = PeerMessage(
    "11111111-1111-4111-8111-111111111111",
    Sender(Provider.CODEX, "22222222-2222-4222-8222-222222222222", "Local review"),
    "A body with punctuation & non-ASCII characters, por ejemplo: café.",
)


class MailboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.mailbox = Mailbox(Path(self.temporary.name) / "mail.sqlite")

    def test_read_without_saved_mail_preserves_cursor(self) -> None:
        self.assertEqual(self.mailbox.read("recipient", 2), MessagePage((), 2))

    def test_read_leaves_missing_mailbox_uncreated(self) -> None:
        self.mailbox.read("recipient", 0)
        self.assertFalse(self.mailbox.path.exists())

    def test_messages_are_addressed_to_exact_recipient(self) -> None:
        self.mailbox.store("recipient", MESSAGE)
        self.assertEqual(self.mailbox.read("another-chat", 0).messages, ())

    def test_cursor_advances_once_per_stored_message(self) -> None:
        self.mailbox.store("recipient", MESSAGE)
        self.mailbox.store("recipient", MESSAGE)
        self.assertEqual(self.mailbox.read("recipient", 1).next_cursor, 2)

    def test_unicode_round_trips(self) -> None:
        self.mailbox.store("recipient", MESSAGE)
        self.assertEqual(self.mailbox.read("recipient", 0).messages[0].message, MESSAGE)

    def test_retention_removes_only_the_oldest_for_this_recipient(self) -> None:
        with patch("claudex.mailbox.RETENTION", 2):
            self.mailbox.store("recipient", MESSAGE)
            self.mailbox.store("another-chat", MESSAGE)
            self.mailbox.store("recipient", MESSAGE)
            self.mailbox.store("recipient", MESSAGE)
        self.assertEqual([row.position for row in self.mailbox.read("recipient", 0).messages], [3, 4])
        self.assertEqual([row.position for row in self.mailbox.read("another-chat", 0).messages], [2])

    def test_storage_names_match_the_shared_model(self) -> None:
        self.assertIn(b'"chat_id":', PEER_MESSAGE.dump_json(MESSAGE))
