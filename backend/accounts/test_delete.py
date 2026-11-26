import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from files.models import StoredFile
from files.storage import open_storage_directory, remove_storage_files, save_upload

from .models import User


class AdminDeleteTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Example7!"
        cls.admin = User.objects.create_user(
            username="DeleteAdmin7", full_name="Администратор", email="admin@example.invalid",
            password=cls.password, is_staff=True,
        )
        cls.member = User.objects.create_user(
            username="DeleteMember7", full_name="Пользователь", email="member@example.invalid",
            password=cls.password,
        )

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "private"
        self.enterContext(self.settings(FILE_STORAGE_ROOT=self.root, PUBLIC_APP_ORIGIN="http://cloud.example"))
        self.client = self.signed_in(self.admin)

    def signed_in(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get(reverse("csrf"))
        response = client.post(reverse("login"), {"username": user.username, "password": self.password},
                               format="json", **self.csrf(client))
        self.assertEqual(response.status_code, 200)
        return client

    def csrf(self, client):
        return {"HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}

    def delete_user(self, user=None, client=None, **headers):
        client = self.client if client is None else client
        user = self.member if user is None else user
        return client.delete(reverse("admin-user-detail", args=[user.pk]), **{**self.csrf(client), **headers})

    def upload(self, owner=None, content=b"file bytes"):
        return save_upload(self.member if owner is None else owner, SimpleUploadedFile("report.pdf", content), "")

    def cleanup(self, delete=False):
        output = StringIO()
        call_command("cleanup_orphan_files", delete=delete, stdout=output, stderr=StringIO())
        return output.getvalue()

    def test_delete_user_without_files_returns_empty_204_then_404(self):
        response = self.delete_user()
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(User.objects.filter(pk=self.member.pk).exists())
        self.assertEqual(self.delete_user().status_code, 404)

    def test_delete_cascades_records_removes_files_and_preserves_neighbors(self):
        files = [self.upload(content=content) for content in (b"one", b"two", b"")]
        paths = [stored.storage_path for stored in files]
        neighbor = self.upload(self.admin, b"untouched")
        neighbor_before = StoredFile.objects.values().get(pk=neighbor.pk)
        self.assertEqual(self.delete_user().status_code, 204)
        self.assertFalse(StoredFile.objects.filter(owner_id=self.member.pk).exists())
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(self.root.is_dir())
        self.assertEqual(neighbor.storage_path.read_bytes(), b"untouched")
        self.assertEqual(StoredFile.objects.values().get(pk=neighbor.pk), neighbor_before)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
        self.assertNotIn(self.member.pk, [row["id"] for row in self.client.get(reverse("admin-users")).json()])

    def test_guest_and_member_cannot_delete(self):
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))
        for client in (guest, self.signed_in(self.member)):
            response = self.delete_user(client=client)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())

    def test_delete_requires_csrf_and_valid_origin(self):
        for headers in ({}, {"HTTP_X_CSRFTOKEN": "z" * 32}, {**self.csrf(self.client), "HTTP_ORIGIN": "https://example.invalid"}):
            response = self.client.delete(reverse("admin-user-detail", args=[self.member.pk]), **headers)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())

    def test_missing_and_invalid_ids_return_safe_json_404(self):
        for user_id in ("0", "9999999", "bad-id", "-1", "9" * 50):
            response = self.client.delete(reverse("admin-user-detail", args=[user_id]), **self.csrf(self.client))
            self.assertEqual(response.status_code, 404)
            self.assertTrue(response.json()["detail"])

    def test_cannot_delete_self_even_if_another_admin_exists(self):
        User.objects.filter(pk=self.member.pk).update(is_staff=True)
        response = self.delete_user(self.admin)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["detail"])
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_last_active_admin_is_protected(self):
        response = self.delete_user(self.admin)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["is_staff"])
        self.assertTrue(User.objects.filter(pk=self.admin.pk, is_staff=True).exists())

    def test_staff_role_not_superuser_controls_delete_permission(self):
        User.objects.filter(pk=self.member.pk).update(is_superuser=True)
        member = self.signed_in(self.member)
        self.assertEqual(self.delete_user(self.admin, client=member).status_code, 403)
        self.assertEqual(self.delete_user().status_code, 204)

    def test_deleting_other_active_or_inactive_admin_preserves_actor(self):
        for active in (True, False):
            user = User.objects.create_user(username=f"OtherAdmin{int(active)}", full_name="Администратор",
                                           email="other@example.invalid", password=self.password,
                                           is_staff=True, is_active=active)
            self.assertEqual(self.delete_user(user).status_code, 204)
        self.assertTrue(User.objects.filter(pk=self.admin.pk, is_staff=True).exists())

    def test_public_link_and_old_session_stop_working(self):
        member = self.signed_in(self.member)
        stored = self.upload()
        link = member.post(reverse("file-share", args=[stored.pk]), **self.csrf(member)).json()["url"]
        guest = APIClient()
        response = guest.get(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"file bytes")
        old_cookie = member.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.delete_user().status_code, 204)
        self.assertEqual(guest.get(link).status_code, 404)
        for path in (reverse("me"), reverse("files"), reverse("file-download", args=[stored.pk])):
            member.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
            self.assertEqual(member.get(path).status_code, 403)

    def test_missing_physical_file_or_directory_does_not_block_deletion(self):
        stored = self.upload()
        path = stored.storage_path
        path.unlink()
        path.parent.rmdir()
        self.assertEqual(self.delete_user().status_code, 204)
        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())

    def test_delete_rejects_body_including_client_paths(self):
        response = self.client.delete(reverse("admin-user-detail", args=[self.member.pk]),
                                      {"path": "/etc", "owner_id": self.admin.pk}, format="json", **self.csrf(self.client))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["detail"])
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())

    def test_database_delete_failure_preserves_user_and_file(self):
        stored = self.upload()
        with patch.object(User, "delete", side_effect=DatabaseError("private failure")), self.assertLogs("accounts.admin_views", level="ERROR"):
            response = self.delete_user()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(stored.storage_path.read_bytes(), b"file bytes")

    def test_database_commit_failure_rolls_back_before_disk_cleanup(self):
        stored = self.upload()
        original_atomic = transaction.atomic

        @contextmanager
        def failing_commit(*args, **kwargs):
            with original_atomic(*args, **kwargs):
                yield
                if kwargs.get("durable"):
                    raise DatabaseError("commit failed")

        with patch("accounts.admin_views.transaction.atomic", failing_commit), self.assertLogs("accounts.admin_views", level="ERROR"):
            response = self.delete_user()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(stored.storage_path.read_bytes(), b"file bytes")

    def test_disk_failure_is_logged_and_recoverable_without_leaking_paths(self):
        stored = self.upload()
        path = stored.storage_path
        with patch("files.storage.os.unlink", side_effect=PermissionError(str(path))), self.assertLogs("accounts.admin_views", level="ERROR") as logs:
            response = self.delete_user()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(User.objects.filter(pk=self.member.pk).exists())
        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertTrue(path.exists())
        self.assertIn("cleanup_orphan_files", "\n".join(logs.output))
        self.assertNotIn(str(path), response.content.decode() + "\n".join(logs.output))
        self.assertIn(self.member.storage_relpath, self.cleanup())
        self.assertTrue(path.exists())
        self.cleanup(delete=True)
        self.assertFalse(path.exists())

    def test_partial_disk_cleanup_can_be_completed_later(self):
        paths = [self.upload().storage_path for _ in range(2)]
        original_unlink = os.unlink
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("test failure")
            return original_unlink(*args, **kwargs)

        with patch("files.storage.os.unlink", fail_second), self.assertLogs("accounts.admin_views", level="ERROR"):
            response = self.delete_user()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(sum(path.exists() for path in paths), 1)
        self.cleanup(delete=True)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_owner_directory_symlink_preserves_outside_and_database(self):
        stored = self.upload()
        path = stored.storage_path
        path.unlink()
        path.parent.rmdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        sentinel = outside / path.name
        sentinel.write_bytes(b"outside")
        path.parent.symlink_to(outside, target_is_directory=True)
        with self.assertLogs("accounts.admin_views", level="ERROR"):
            response = self.delete_user()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_file_symlink_and_corrupted_relative_path_are_rejected(self):
        stored = self.upload()
        path = stored.storage_path
        outside = self.root.parent / "outside"
        outside.write_bytes(b"outside")
        path.unlink()
        path.symlink_to(outside)
        with self.assertLogs("accounts.admin_views", level="ERROR"):
            self.assertEqual(self.delete_user().status_code, 500)
        path.unlink()
        path.write_bytes(b"original")
        neighbor = self.upload(self.admin)
        StoredFile.objects.filter(pk=stored.pk).update(relative_path=f"{self.admin.storage_relpath}/{uuid4().hex}")
        with self.assertLogs("accounts.admin_views", level="ERROR"):
            self.assertEqual(self.delete_user().status_code, 500)
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())
        self.assertTrue(neighbor.storage_path.exists())
        for unsafe in ("../outside", "", "/etc"):
            with self.assertRaises(ValueError), open_storage_directory(unsafe):
                pass

    def test_directory_replacement_after_commit_cannot_delete_outside_files(self):
        stored = self.upload()
        path = stored.storage_path
        moved = self.root.parent / "moved"
        outside = self.root.parent / "outside"
        outside.mkdir()
        sentinel = outside / path.name
        sentinel.write_bytes(b"outside")

        def swap_then_remove(owner_fd, names):
            path.parent.rename(moved)
            path.parent.symlink_to(outside, target_is_directory=True)
            remove_storage_files(owner_fd, names)

        with patch("accounts.admin_views.remove_storage_files", swap_then_remove):
            self.assertEqual(self.delete_user().status_code, 204)
        self.assertFalse((moved / path.name).exists())
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_cleanup_skips_active_users_and_dry_run_never_deletes(self):
        stored = self.upload()
        orphan = self.root / uuid4().hex
        orphan.mkdir()
        leftover = orphan / uuid4().hex
        leftover.write_bytes(b"leftover")
        self.assertIn(orphan.name, self.cleanup())
        self.assertTrue(leftover.exists())
        self.cleanup(delete=True)
        self.assertFalse(leftover.exists())
        self.assertEqual(stored.storage_path.read_bytes(), b"file bytes")
        self.assertTrue(orphan.is_dir())
        self.assertTrue(self.root.is_dir())

    def test_cleanup_rejects_symlink_directory_and_nonregular_entries(self):
        self.root.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        sentinel = outside / uuid4().hex
        sentinel.write_bytes(b"outside")
        link = self.root / uuid4().hex
        link.symlink_to(outside, target_is_directory=True)
        for delete in (False, True):
            with self.assertRaises(CommandError):
                self.cleanup(delete=delete)
        self.assertEqual(sentinel.read_bytes(), b"outside")
        link.unlink()
        link.mkdir()
        (link / uuid4().hex).symlink_to(sentinel)
        with self.assertRaises(CommandError):
            self.cleanup(delete=True)
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_cleanup_handles_missing_root_and_unknown_files_without_recursion(self):
        self.assertIn("Хранилище отсутствует", self.cleanup())
        folder = self.root / uuid4().hex
        folder.mkdir(parents=True)
        nested = folder / "unknown"
        nested.mkdir()
        sentinel = nested / "keep.txt"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(CommandError):
            self.cleanup(delete=True)
        self.assertEqual(sentinel.read_bytes(), b"keep")


class ConcurrentUserDeletionTests(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(FILE_STORAGE_ROOT=Path(directory) / "private"))

    @classmethod
    def tearDownClass(cls):
        # Сохраняем данные миграций для следующего запуска с --keepdb после flush.
        connection.creation.deserialize_db_from_string(connection._test_serialized_contents)
        super().tearDownClass()

    def test_competing_deletes_keep_one_active_admin(self):
        password = "Example7!"
        users = [User.objects.create_user(username=f"Concurrent{index}", full_name="Администратор",
                                         email="parallel@example.invalid", password=password, is_staff=True)
                 for index in range(2)]
        clients = []
        for user in users:
            client = APIClient(enforce_csrf_checks=True)
            client.force_login(user)
            client.get(reverse("csrf"))
            clients.append(client)
        barrier = Barrier(2)

        def delete_other(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                client = clients[index]
                return client.delete(reverse("admin-user-detail", args=[users[1 - index].pk]),
                                     HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as workers:
            statuses = list(workers.map(delete_other, (0, 1)))
        self.assertEqual(sorted(statuses), [204, 403])
        self.assertEqual(User.objects.filter(is_staff=True, is_active=True).count(), 1)
