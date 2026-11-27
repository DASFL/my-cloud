from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.http import FileResponse
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from accounts.models import User

from .models import StoredFile
from .storage import save_upload


class AdminFileAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Example7!"
        cls.admin, cls.owner, cls.other = [User.objects.create_user(
            username=name, full_name=name, email=f"{name}@example.invalid",
            password=cls.password, is_staff=index == 0,
        ) for index, name in enumerate(("AdminFiles7", "OwnerFiles7", "OtherFiles7"))]

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "private"
        self.enterContext(self.settings(FILE_STORAGE_ROOT=self.root, PUBLIC_APP_ORIGIN="https://cloud.example.com"))
        self.client = self.sign_in(self.admin)

    def sign_in(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get(reverse("csrf"))
        response = client.post(reverse("login"), {"username": user.username, "password": self.password},
                               format="json", **self.csrf(client))
        self.assertEqual(response.status_code, 200)
        return client

    def csrf(self, client):
        return {"HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}

    def url(self, name="admin-files", owner=None, file_id=None):
        args = [self.owner.pk if owner is None else owner]
        if file_id is not None:
            args.append(file_id)
        return reverse(name, args=args)

    def create_file(self, owner=None, content=b"original bytes"):
        return save_upload(self.owner if owner is None else owner,
                           SimpleUploadedFile("report.txt", content), "Исходный комментарий")

    def upload(self, selected_owner=None, client=None, **fields):
        client = self.client if client is None else client
        return client.post(self.url(owner=selected_owner), {
            "file": SimpleUploadedFile("report.txt", b"uploaded bytes"), **fields,
        }, format="multipart", **self.csrf(client))

    def file_request(self, method, stored, data=None, owner=None, name="admin-file-detail", client=None):
        client = self.client if client is None else client
        headers = {} if method == "get" else self.csrf(client)
        return getattr(client, method)(self.url(name, owner, stored.pk), data, format="json", **headers)

    def operation_requests(self, client, stored, owner=None):
        yield client.get(self.url(owner=owner))
        yield self.upload(owner, client)
        yield self.file_request("patch", stored, {"comment": "Изменить"}, owner, client=client)
        yield self.file_request("get", stored, owner=owner, name="admin-file-download", client=client)
        yield self.file_request("delete", stored, owner=owner, client=client)
        yield self.file_request("post", stored, owner=owner, name="admin-file-share", client=client)

    def read_download(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response, FileResponse)
        return b"".join(response.streaming_content)

    def totals(self):
        return next(row for row in self.client.get(reverse("admin-users")).json() if row["id"] == self.owner.pk)

    def test_admin_lists_selected_owner_metadata_newest_first(self):
        first = self.create_file()
        second = self.create_file(content=b"second")
        self.create_file(self.other)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()], [str(second.pk), str(first.pk)])
        row = response.json()[0]
        self.assertEqual(set(row), {"id", "original_name", "comment", "size_bytes", "created_at", "last_downloaded_at"})
        self.assertEqual(row["original_name"], "report.txt")
        self.assertEqual(row["comment"], second.comment)
        self.assertEqual(row["size_bytes"], 6)
        self.assertIsNone(row["last_downloaded_at"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_admin_empty_and_own_storage_are_selected_explicitly(self):
        own = self.create_file(self.admin)
        self.assertEqual(self.client.get(self.url()).json(), [])
        self.assertEqual([row["id"] for row in self.client.get(self.url(owner=self.admin.pk)).json()], [str(own.pk)])
        self.assertEqual(self.client.get(self.url(), {"owner": self.admin.pk, "owner_id": self.admin.pk}).json(), [])
        response = self.upload(selected_owner=self.admin.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(StoredFile.objects.get(pk=response.json()["id"]).owner_id, self.admin.pk)

    def test_guest_cannot_use_any_admin_file_operation(self):
        stored = self.create_file()
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))
        for response in self.operation_requests(guest, stored):
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.assertEqual(stored.storage_path.read_bytes(), b"original bytes")
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_regular_user_cannot_use_admin_routes_even_as_superuser(self):
        User.objects.filter(pk=self.owner.pk).update(is_superuser=True)
        client = self.sign_in(self.owner)
        stored = self.create_file()
        for response in self.operation_requests(client, stored):
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")
        self.assertIsNone(stored.share_token)

    def test_admin_upload_assigns_owner_and_is_visible_in_owner_api_and_totals(self):
        response = self.upload(comment="Загружено администратором")
        self.assertEqual(response.status_code, 201)
        stored = StoredFile.objects.get(pk=response.json()["id"])
        self.assertEqual(stored.owner_id, self.owner.pk)
        self.assertEqual(stored.size_bytes, len(b"uploaded bytes"))
        self.assertEqual(stored.storage_path.read_bytes(), b"uploaded bytes")
        self.assertTrue(stored.relative_path.startswith(self.owner.storage_relpath + "/"))
        owner = self.sign_in(self.owner)
        listed = owner.get(reverse("files")).json()
        self.assertEqual(listed, [response.json()])
        self.assertEqual((self.totals()["files_count"], self.totals()["total_size_bytes"]), (1, stored.size_bytes))
        own_api = self.client.get(reverse("files"), {"owner": self.owner.pk}).json()
        self.assertEqual(own_api, [])

    def test_equal_names_in_different_storages_do_not_collide(self):
        first = self.upload()
        second = self.upload(selected_owner=self.other.pk)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        files = list(StoredFile.objects.order_by("owner_id"))
        self.assertEqual(files[0].original_name, files[1].original_name)
        self.assertNotEqual(files[0].relative_path, files[1].relative_path)
        self.assertTrue(all(stored.storage_path.read_bytes() == b"uploaded bytes" for stored in files))

    def test_admin_partial_edits_are_visible_to_owner_without_changing_system_fields(self):
        stored = self.create_file()
        path = stored.storage_path
        before = StoredFile.objects.values().get(pk=stored.pk)
        response = self.file_request("patch", stored, {"original_name": "Новый отчёт.pdf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comment"], before["comment"])
        self.assertEqual(self.file_request("patch", stored, {"comment": "Новый комментарий"}).status_code, 200)
        after = StoredFile.objects.values().get(pk=stored.pk)
        self.assertEqual(after, {**before, "original_name": "Новый отчёт.pdf", "comment": "Новый комментарий"})
        owner = self.sign_in(self.owner)
        self.assertEqual(owner.get(reverse("files")).json()[0]["original_name"], "Новый отчёт.pdf")
        self.assertEqual(path.read_bytes(), b"original bytes")
        self.assertEqual(self.totals()["total_size_bytes"], stored.size_bytes)
        self.assertEqual(self.file_request("patch", stored, {"comment": ""}).json()["comment"], "")

    def test_admin_download_streams_current_name_and_updates_timestamp(self):
        stored = self.create_file(content=b"\x00binary\xff")
        self.file_request("patch", stored, {"original_name": "Отчёт с пробелами.bin"})
        before = StoredFile.objects.values().get(pk=stored.pk)
        response = self.file_request("get", stored, name="admin-file-download")
        self.assertEqual(unquote(response["Content-Disposition"].split("filename*=utf-8''")[1]), "Отчёт с пробелами.bin")
        self.assertEqual(self.read_download(response), b"\x00binary\xff")
        stored.refresh_from_db()
        self.assertIsNotNone(stored.last_downloaded_at)
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), {**before, "last_downloaded_at": stored.last_downloaded_at})

    def test_admin_delete_removes_disk_and_row_and_updates_lists_and_totals(self):
        stored = self.create_file()
        path = stored.storage_path
        neighbor = self.create_file(self.other)
        before = StoredFile.objects.values().get(pk=neighbor.pk)
        response = self.file_request("delete", stored)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(path.exists())
        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(self.client.get(self.url()).json(), [])
        self.assertEqual(self.sign_in(self.owner).get(reverse("files")).json(), [])
        self.assertEqual((self.totals()["files_count"], self.totals()["total_size_bytes"]), (0, 0))
        self.assertEqual(self.file_request("delete", stored).status_code, 404)
        self.assertEqual(StoredFile.objects.values().get(pk=neighbor.pk), before)
        self.assertEqual(neighbor.storage_path.read_bytes(), b"original bytes")

    def test_admin_share_is_stable_public_and_disappears_after_delete(self):
        stored = self.create_file()
        response = self.file_request("post", stored, name="admin-file-share")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.file_request("post", stored, name="admin-file-share").json(), response.json())
        url = urlsplit(response.json()["url"]).path
        self.assertEqual(self.read_download(APIClient().get(url)), b"original bytes")
        stored.refresh_from_db()
        listed = self.client.get(self.url())
        self.assertNotIn(stored.share_token, listed.content.decode())
        self.assertEqual(self.file_request("delete", stored).status_code, 204)
        self.assertEqual(APIClient().get(url).status_code, 404)

    def test_file_must_belong_to_selected_user_for_every_object_operation(self):
        stored = self.create_file(self.other)
        before = StoredFile.objects.values().get(pk=stored.pk)
        for method, name, data in (("patch", "admin-file-detail", {"comment": "Чужой"}),
                                   ("delete", "admin-file-detail", None),
                                   ("get", "admin-file-download", None),
                                   ("post", "admin-file-share", None)):
            response = self.file_request(method, stored, data, name=name)
            missing = getattr(self.client, method)(self.url(name, file_id=uuid4()), data, format="json", **self.csrf(self.client))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), missing.json())
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)
        self.assertEqual(stored.storage_path.read_bytes(), b"original bytes")

    def test_missing_or_invalid_user_is_404_for_all_operations(self):
        stored = self.create_file()
        for user_id in ("99999999", "bad-id", "-1", "9" * 50):
            for response in self.operation_requests(self.client, stored, owner=user_id):
                self.assertEqual(response.status_code, 404)
                self.assertTrue(response.json()["detail"])
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_upload_and_patch_reject_owner_paths_and_system_fields(self):
        stored = self.create_file()
        before = StoredFile.objects.values().get(pk=stored.pk)
        fields = {"owner_id": self.other.pk, "owner": self.other.pk, "relative_path": "../outside",
                  "id": str(uuid4()), "size_bytes": 0, "created_at": "2026-01-01", "share_token": "secret"}
        for name, value in fields.items():
            response = self.upload(**{name: value})
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()[name])
            response = self.file_request("patch", stored, {"comment": "Нельзя", name: value})
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()[name])
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_unsafe_requests_require_csrf_and_valid_origin(self):
        stored = self.create_file()
        before = StoredFile.objects.values().get(pk=stored.pk)
        for headers in ({}, {"HTTP_X_CSRFTOKEN": "z" * 32}, {**self.csrf(self.client), "HTTP_ORIGIN": "https://example.invalid"}):
            cases = (("post", self.url(), {"file": SimpleUploadedFile("extra.txt", b"extra")}, "multipart"),
                     ("patch", self.url("admin-file-detail", file_id=stored.pk), {"comment": "Изменить"}, "json"),
                     ("delete", self.url("admin-file-detail", file_id=stored.pk), None, "json"),
                     ("post", self.url("admin-file-share", file_id=stored.pk), None, "json"))
            for method, url, data, format in cases:
                response = getattr(self.client, method)(url, data, format=format, **headers)
                self.assertEqual(response.status_code, 403)
                self.assertTrue(response.json()["detail"])
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_revoked_admin_loses_all_admin_routes_in_existing_session(self):
        stored = self.create_file()
        second = User.objects.create_user(username="SecondAdmin7", full_name="Администратор",
                                         email="second@example.invalid", password=self.password, is_staff=True)
        client = self.sign_in(second)
        response = client.patch(reverse("admin-user-detail", args=[self.admin.pk]), {"is_staff": False},
                                format="json", **self.csrf(client))
        self.assertEqual(response.status_code, 200)
        for response in self.operation_requests(self.client, stored):
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.client.get(reverse("me")).json()["is_staff"])
        self.assertEqual(self.client.get(reverse("files")).status_code, 200)
        self.assertEqual(stored.storage_path.read_bytes(), b"original bytes")

    def test_admin_routes_keep_existing_path_and_symlink_protection(self):
        stored = self.create_file()
        path = stored.storage_path
        outside = self.root.parent / "outside"
        outside.write_bytes(b"outside")
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.file_request("get", stored, name="admin-file-download").status_code, 404)
        self.assertEqual(self.file_request("delete", stored).status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(outside.read_bytes(), b"outside")
        response = self.file_request("patch", stored, {"original_name": "../escape", "comment": "Не применять"})
        self.assertEqual(response.status_code, 400)
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")

    def test_admin_upload_rejects_symlink_owner_directory(self):
        self.root.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.root / self.owner.storage_relpath).symlink_to(outside, target_is_directory=True)
        response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(StoredFile.objects.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_admin_write_errors_leave_no_metadata_or_files_and_hide_paths(self):
        private_error = str(self.root / self.owner.storage_relpath)
        for target, error in (("files.models.StoredFile.save", DatabaseError(private_error)),
                              ("django.core.files.uploadedfile.InMemoryUploadedFile.chunks", OSError(private_error))):
            with patch(target, side_effect=error), self.assertLogs("files.views", level="ERROR") as logs:
                response = self.upload()
            self.assertEqual(response.status_code, 500)
            self.assertNotIn(private_error, response.content.decode() + "\n".join(logs.output))
            self.assertFalse(StoredFile.objects.exists())
            self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])
