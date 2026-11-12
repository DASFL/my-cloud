from pathlib import Path, PurePosixPath
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import StoredFile


class StoredFileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(
            username="Alice7", full_name="Алиса", email="alice@example.com"
        )
        cls.other_owner = user_model.objects.create_user(
            username="Boris8", full_name="Борис", email="boris@example.com"
        )

    def create_file(self, **overrides):
        fields = {"owner": self.owner, "original_name": "report.pdf", "size_bytes": 42}
        return StoredFile.objects.create(**{**fields, **overrides})

    def test_saves_metadata_and_owner(self):
        size = 5 * 1024**3 + 42
        before = timezone.now()
        saved = self.create_file(size_bytes=size, comment="Отчёт за месяц")
        stored = self.owner.stored_files.get(pk=saved.pk)

        self.assertIsInstance(stored.pk, UUID)
        self.assertEqual(stored.owner, self.owner)
        self.assertEqual(stored.original_name, "report.pdf")
        self.assertEqual(stored.size_bytes, size)
        self.assertEqual(stored.comment, "Отчёт за месяц")
        self.assertLessEqual(before, stored.created_at)
        self.assertLessEqual(stored.created_at, timezone.now())

    def test_optional_fields_and_zero_byte_size(self):
        stored = self.create_file(size_bytes=0)
        another = self.create_file()
        stored.refresh_from_db()

        self.assertEqual(stored.size_bytes, 0)
        self.assertEqual(stored.comment, "")
        self.assertIsNone(stored.last_downloaded_at)
        self.assertIsNone(stored.share_token)
        self.assertIsNone(another.share_token)

    def test_equal_names_use_distinct_paths_and_owner_directories(self):
        first = self.create_file()
        same_owner = self.create_file()
        other_owner = self.create_file(owner=self.other_owner)

        for stored in (first, same_owner, other_owner):
            self.assertEqual(stored.original_name, "report.pdf")
            self.assertEqual(
                PurePosixPath(stored.relative_path).parts,
                (stored.owner.storage_relpath, stored.pk.hex),
            )
        self.assertEqual(len({first.relative_path, same_owner.relative_path, other_owner.relative_path}), 3)
        self.assertNotEqual(first.owner.storage_relpath, other_owner.owner.storage_relpath)

    def test_original_name_cannot_affect_the_physical_path(self):
        stored = self.create_file(original_name="../../outside.txt")

        with TemporaryDirectory() as directory, self.settings(FILE_STORAGE_ROOT=directory):
            root = Path(directory).resolve()
            expected = root / self.owner.storage_relpath / stored.pk.hex
            self.assertEqual(stored.storage_path, expected)
            self.assertFalse(expected.exists())

            stored.original_name = "/tmp/renamed.txt"
            stored.save(update_fields=["original_name"])
            stored.refresh_from_db()
            self.assertEqual(stored.storage_path, expected)

    def test_rejects_paths_outside_the_owner_directory(self):
        stored = self.create_file()
        with TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir()
            (root / self.owner.storage_relpath).symlink_to(directory, target_is_directory=True)

            with self.settings(FILE_STORAGE_ROOT=root), self.assertRaises(ValueError):
                _ = stored.storage_path

        stored.relative_path = f"{self.other_owner.storage_relpath}/{stored.pk.hex}"
        with self.assertRaises(ValueError):
            _ = stored.storage_path

    def test_database_rejects_duplicate_or_invalid_path(self):
        first = self.create_file()
        other = self.create_file()
        for path in (first.relative_path, "../outside.txt", "/tmp/report.pdf"):
            with self.subTest(path=path):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    StoredFile.objects.filter(pk=other.pk).update(relative_path=path)

    def test_database_rejects_duplicate_share_token(self):
        token = token_urlsafe(32)
        first = self.create_file(share_token=token)
        first.refresh_from_db()
        self.assertEqual(first.share_token, token)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_file(share_token=token)

    def test_database_rejects_missing_name_and_negative_size(self):
        for fields in ({"original_name": ""}, {"size_bytes": -1}):
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.create_file(**fields)

    def test_deleting_owner_removes_only_their_file_records(self):
        first = self.create_file()
        other = self.create_file(owner=self.other_owner)

        self.owner.delete()

        self.assertFalse(StoredFile.objects.filter(pk=first.pk).exists())
        self.assertTrue(StoredFile.objects.filter(pk=other.pk).exists())
