from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from peer_mailbox import Mailbox, MESSAGE_FORMAT, MessagePage, PeerMessage, Provider, Sender


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

    def test_preexisting_mailbox_format_still_parses(self) -> None:
        legacy_json = (
            '{"message_id":"11111111-1111-4111-8111-111111111111",'
            '"sender":{"peer":"Local review","provider":"codex",'
            '"thread":"22222222-2222-4222-8222-222222222222"},"body":"Earlier message"}'
        )
        self.mailbox.store("recipient", MESSAGE)
        with sqlite3.connect(self.mailbox.path) as connection:
            connection.execute("UPDATE mail SET message=?", (legacy_json,))
        self.assertEqual(self.mailbox.read("recipient", 0).messages[0].message.sender, MESSAGE.sender)

    def test_storage_keeps_the_existing_sender_keys(self) -> None:
        self.assertIn(b'"thread":', MESSAGE_FORMAT.dump_json(MESSAGE, by_alias=True))
