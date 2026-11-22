import logging
from datetime import timedelta
from io import StringIO
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from unittest.mock import PropertyMock, patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.db import DatabaseError, connection
from django.http import FileResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient, APITestCase

from config.log_filters import RedactShareTokens

from .models import StoredFile


class FileAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.password = "Example7!"
        cls.owner = user_model.objects.create_user(
            username="Alice7", full_name="Алиса", email="alice@example.com", password=cls.password
        )
        cls.other = user_model.objects.create_user(
            username="Boris8", full_name="Борис", email="boris@example.com", password=cls.password
        )

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory) / "private"
        self.enterContext(self.settings(FILE_STORAGE_ROOT=self.root))
        self.url = reverse("files")
        self.client = self.signed_in_client(self.owner)

    def signed_in_client(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get(reverse("csrf"))
        response = client.post(
            reverse("login"), {"username": user.username, "password": self.password},
            format="json", **self.csrf_header(client),
        )
        self.assertEqual(response.status_code, 200)
        return client

    def csrf_header(self, client):
        return {"HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}

    def upload(self, content=b"report contents", client=None, **fields):
        client = self.client if client is None else client
        data = {"file": SimpleUploadedFile("report.pdf", content), **fields}
        return client.post(self.url, data, format="multipart", **self.csrf_header(client))

    def saved_paths(self):
        return [path for path in self.root.rglob("*") if path.is_file()]

    def assert_nothing_saved(self):
        self.assertFalse(StoredFile.objects.exists())
        self.assertEqual(self.saved_paths(), [])

    def edit_target(self):
        response = self.upload(comment="Исходный комментарий")
        self.assertEqual(response.status_code, 201)
        return StoredFile.objects.get(pk=response.json()["id"])

    def patch_file(self, file_id, data, client=None):
        client = self.client if client is None else client
        return client.patch(
            reverse("file-detail", args=[file_id]), data, format="json", **self.csrf_header(client)
        )

    def download(self, file_id, client=None, **headers):
        client = self.client if client is None else client
        return client.get(reverse("file-download", args=[file_id]), **headers)

    def read_download(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response, FileResponse)
        content = b"".join(response.streaming_content)
        self.assertTrue(response.closed)
        return content

    def delete_file(self, file_id, client=None, data=None):
        client = self.client if client is None else client
        return client.delete(
            reverse("file-detail", args=[file_id]), data=data, format="json", **self.csrf_header(client)
        )

    def share(self, file_id, client=None, **headers):
        client = self.client if client is None else client
        with self.settings(PUBLIC_APP_ORIGIN="https://cloud.example.com/"):
            return client.post(
                reverse("file-share", args=[file_id]), **self.csrf_header(client), **headers
            )

    def test_upload_saves_bytes_and_public_metadata(self):
        content = b"\x00report\xff"
        response = self.upload(content, comment="Отчёт")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Content-Type"], "application/json")
        data = response.json()
        stored = StoredFile.objects.get(pk=data["id"])
        self.assertEqual(stored.owner, self.owner)
        self.assertEqual(stored.relative_path, f"{self.owner.storage_relpath}/{stored.pk.hex}")
        self.assertEqual(stored.storage_path.read_bytes(), content)
        self.assertEqual(stored.original_name, "report.pdf")
        self.assertEqual(stored.comment, "Отчёт")
        self.assertEqual(stored.size_bytes, len(content))
        self.assertIsNone(stored.share_token)
        self.assertEqual(set(data), {
            "id", "original_name", "comment", "size_bytes", "created_at", "last_downloaded_at",
        })
        self.assertEqual(data["original_name"], stored.original_name)
        self.assertEqual(data["comment"], stored.comment)
        self.assertEqual(data["size_bytes"], stored.size_bytes)
        self.assertEqual(parse_datetime(data["created_at"]), stored.created_at)
        self.assertIsNone(data["last_downloaded_at"])

    def test_upload_accepts_empty_file_without_comment(self):
        response = self.upload(b"")

        self.assertEqual(response.status_code, 201)
        stored = StoredFile.objects.get()
        self.assertEqual(stored.comment, "")
        self.assertEqual(stored.storage_path.read_bytes(), b"")
        self.assertEqual(response.json()["size_bytes"], 0)

    def test_equal_names_do_not_conflict_across_uploads_or_owners(self):
        other_client = self.signed_in_client(self.other)
        paths = set()
        for client, owner, content in (
            (self.client, self.owner, b"first"),
            (self.client, self.owner, b"second"),
            (other_client, self.other, b"third"),
        ):
            response = self.upload(content, client=client)
            self.assertEqual(response.status_code, 201)
            stored = StoredFile.objects.get(pk=response.json()["id"])
            self.assertEqual(stored.owner, owner)
            self.assertEqual(stored.original_name, "report.pdf")
            self.assertEqual(stored.storage_path.read_bytes(), content)
            paths.add(stored.relative_path)
        self.assertEqual(len(paths), 3)

    def test_guest_cannot_upload_or_list_even_with_csrf_cookie(self):
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))

        self.assertEqual(self.upload(client=guest).status_code, 403)
        self.assertEqual(guest.get(self.url).status_code, 403)
        self.assert_nothing_saved()

    def test_upload_requires_csrf_and_rejects_foreign_origin(self):
        for headers in ({}, {**self.csrf_header(self.client), "HTTP_ORIGIN": "https://example.invalid"}):
            response = self.client.post(
                self.url, {"file": SimpleUploadedFile("report.pdf", b"data")},
                format="multipart", **headers,
            )
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.assert_nothing_saved()

    def test_upload_requires_a_file(self):
        response = self.client.post(
            self.url, {"comment": "Только комментарий"},
            format="multipart", **self.csrf_header(self.client),
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["file"])
        self.assert_nothing_saved()

    def test_upload_rejects_system_fields(self):
        extra = {
            "owner": self.other.pk, "owner_id": self.other.pk, "relative_path": "custom-path",
            "size_bytes": 100, "share_token": "provided-token", "original_name": "custom-name",
        }
        response = self.upload(**extra)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), set(extra))
        self.assert_nothing_saved()

    def test_upload_rejects_non_multipart_and_multiple_files(self):
        response = self.client.post(
            self.url, {"file": "report.pdf"}, format="json", **self.csrf_header(self.client),
        )
        self.assertEqual(response.status_code, 415)
        self.assertTrue(response.json()["detail"])

        response = self.upload(file=[SimpleUploadedFile("a.txt", b"a"), SimpleUploadedFile("b.txt", b"b")])
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["file"])
        self.assert_nothing_saved()

    def test_upload_respects_configured_size_limit(self):
        with self.settings(FILE_UPLOAD_MAX_BYTES=4):
            response = self.upload(b"12345")
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["file"])
            self.assert_nothing_saved()
            self.assertEqual(self.upload(b"1234").status_code, 201)

    def test_upload_uses_basename_as_metadata_and_never_as_path(self):
        response = self.client.post(
            self.url,
            b'--test\r\nContent-Disposition: form-data; name="file"; filename="../../outside.txt"\r\n'
            b'Content-Type: text/plain\r\n\r\ntest bytes\r\n--test--\r\n',
            content_type="multipart/form-data; boundary=test", **self.csrf_header(self.client),
        )

        self.assertEqual(response.status_code, 201)
        stored = StoredFile.objects.get()
        self.assertEqual(stored.original_name, "outside.txt")
        self.assertEqual(stored.storage_path, self.root.resolve() / self.owner.storage_relpath / stored.pk.hex)
        self.assertEqual(self.saved_paths(), [self.root / stored.relative_path])

    def test_upload_rejects_owner_directory_symlink(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.root.mkdir()
        (self.root / self.owner.storage_relpath).symlink_to(outside, target_is_directory=True)

        response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json()["detail"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_nothing_saved()

    def test_list_is_empty_when_only_other_users_have_files(self):
        self.assertEqual(self.upload(client=self.signed_in_client(self.other)).status_code, 201)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_returns_own_metadata_newest_first_and_ignores_owner_parameters(self):
        old = self.upload(b"old", comment="Старый").json()
        new = self.upload(b"newer", comment="Новый").json()
        self.assertEqual(self.upload(client=self.signed_in_client(self.other)).status_code, 201)
        downloaded_at = timezone.now()
        token = token_urlsafe(32)
        StoredFile.objects.filter(pk=old["id"]).update(created_at=downloaded_at - timedelta(days=1))
        StoredFile.objects.filter(pk=new["id"]).update(last_downloaded_at=downloaded_at, share_token=token)

        response = self.client.get(self.url, {"owner": self.other.pk, "owner_id": self.other.pk})

        self.assertEqual(response.status_code, 200)
        newest, oldest = response.json()
        self.assertEqual(set(newest), set(new))
        self.assertEqual(set(oldest), set(old))
        self.assertEqual({**newest, "last_downloaded_at": None}, new)
        self.assertEqual({**oldest, "created_at": old["created_at"]}, old)
        self.assertEqual(parse_datetime(newest["last_downloaded_at"]), downloaded_at)
        self.assertGreater(parse_datetime(newest["created_at"]), parse_datetime(oldest["created_at"]))
        self.assertNotIn(token, response.content.decode())
        self.assertNotIn(self.owner.storage_relpath, response.content.decode())

    def test_write_failure_removes_partial_file_and_rolls_back_metadata(self):
        def broken_chunks():
            yield b"partial"
            raise OSError("test write failure")

        with patch.object(InMemoryUploadedFile, "chunks", return_value=broken_chunks()):
            response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Не удалось сохранить файл."})
        self.assert_nothing_saved()

    def test_metadata_insert_failure_leaves_no_file(self):
        with patch.object(StoredFile, "save", side_effect=DatabaseError("test insert failure")):
            response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Не удалось сохранить файл."})
        self.assert_nothing_saved()

    def test_transaction_failure_removes_file_already_written(self):
        original_commit = connection.savepoint_commit

        def fail_once(savepoint):
            paths = self.saved_paths()
            if paths:
                self.assertEqual(paths[0].read_bytes(), b"report contents")
                commit.side_effect = original_commit
                raise DatabaseError("test failure when finishing transaction")
            return original_commit(savepoint)

        with patch.object(connection, "savepoint_commit", side_effect=fail_once) as commit:
            response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Не удалось сохранить файл."})
        self.assert_nothing_saved()

    def test_upload_does_not_overwrite_or_delete_an_existing_file(self):
        file_id = uuid4()
        path = self.root / self.owner.storage_relpath / file_id.hex
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing content")

        with patch("files.storage.StoredFile", side_effect=lambda **fields: StoredFile(id=file_id, **fields)):
            response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertFalse(StoredFile.objects.exists())
        self.assertEqual(path.read_bytes(), b"existing content")

    def test_rename_updates_only_display_name(self):
        stored = self.edit_target()
        stored.last_downloaded_at = timezone.now()
        stored.share_token = token_urlsafe(32)
        stored.save(update_fields=["last_downloaded_at", "share_token"])
        path = stored.storage_path
        content = path.read_bytes()
        before = StoredFile.objects.values().get(pk=stored.pk)
        previous_metadata = self.client.get(self.url).json()[0]
        name = "Отчёт за ноябрь 2026.pdf"

        response = self.patch_file(stored.pk, {"original_name": name})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {**previous_metadata, "original_name": name})
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), {**before, "original_name": name})
        self.assertEqual(StoredFile.objects.count(), 1)
        stored.refresh_from_db()
        self.assertEqual(stored.storage_path, path)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.saved_paths(), [self.root / stored.relative_path])

    def test_patch_updates_comment_without_renaming(self):
        stored = self.edit_target()
        before = StoredFile.objects.values().get(pk=stored.pk)

        response = self.patch_file(stored.pk, {"comment": "Новый комментарий"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comment"], "Новый комментарий")
        self.assertEqual(
            StoredFile.objects.values().get(pk=stored.pk), {**before, "comment": "Новый комментарий"}
        )

    def test_patch_updates_both_fields(self):
        stored = self.edit_target()
        changes = {"original_name": "Отчёт.xlsx", "comment": "Новая версия"}

        response = self.patch_file(stored.pk, changes)

        self.assertEqual(response.status_code, 200)
        stored.refresh_from_db()
        self.assertEqual(stored.original_name, changes["original_name"])
        self.assertEqual(stored.comment, changes["comment"])
        self.assertEqual(self.client.get(self.url).json(), [response.json()])

    def test_patch_clears_comment(self):
        stored = self.edit_target()

        response = self.patch_file(stored.pk, {"comment": ""})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comment"], "")
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "")

    def test_empty_patch_leaves_metadata_unchanged(self):
        stored = self.edit_target()
        before = self.client.get(self.url).json()[0]

        response = self.patch_file(stored.pk, {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), before)
        self.assertEqual(self.client.get(self.url).json(), [before])

    def test_patch_rejects_blank_names_and_dot_segments(self):
        stored = self.edit_target()
        for name in ("", "   ", ".", ".."):
            with self.subTest(name=name):
                response = self.patch_file(stored.pk, {"original_name": name})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["original_name"])

    def test_patch_rejects_path_separators_and_control_characters(self):
        stored = self.edit_target()
        for name in (
            "../report.pdf", "dir\\report.pdf", "/tmp/report.pdf",
            "a\nb.pdf", "a\x00b.pdf", "a\x7fb.pdf", "a\u202eb.pdf",
        ):
            with self.subTest(name=name):
                response = self.patch_file(stored.pk, {"original_name": name})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["original_name"])

    def test_patch_enforces_model_name_length(self):
        stored = self.edit_target()
        limit = StoredFile._meta.get_field("original_name").max_length
        valid_name = "я" * limit

        self.assertEqual(self.patch_file(stored.pk, {"original_name": valid_name}).status_code, 200)
        response = self.patch_file(stored.pk, {"original_name": valid_name + "я"})

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["original_name"])
        stored.refresh_from_db()
        self.assertEqual(stored.original_name, valid_name)

    def test_patch_requires_strings_for_name_and_comment(self):
        stored = self.edit_target()
        for field in ("original_name", "comment"):
            for value in (None, 12, True, [], {}):
                with self.subTest(field=field, value=value):
                    response = self.patch_file(stored.pk, {field: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertTrue(response.json()[field])

    def test_patch_rejects_system_fields(self):
        stored = self.edit_target()
        before = StoredFile.objects.values().get(pk=stored.pk)
        fields = {
            "id": str(uuid4()), "owner": self.other.pk, "owner_id": self.other.pk,
            "relative_path": "elsewhere", "size_bytes": 1, "created_at": None,
            "last_downloaded_at": None, "share_token": "provided-token", "extra": "unsupported",
        }

        response = self.patch_file(stored.pk, {"comment": "Изменённый", **fields})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), set(fields))
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)

    def test_patch_hides_foreign_files_even_from_staff(self):
        stored = self.edit_target()
        other_client = self.signed_in_client(self.other)
        response = self.patch_file(stored.pk, {"comment": "Чужой"}, client=other_client)
        self.assertEqual(response.status_code, 404)

        get_user_model().objects.filter(pk=self.other.pk).update(is_staff=True)
        staff_response = self.patch_file(stored.pk, {"comment": "Чужой"}, client=other_client)
        missing = self.patch_file(uuid4(), {"comment": "Чужой"}, client=other_client)
        self.assertEqual(staff_response.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(response.json(), missing.json())
        self.assertEqual(staff_response.json(), missing.json())
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")

    def test_guest_cannot_patch_even_with_csrf_cookie(self):
        stored = self.edit_target()
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))

        response = self.patch_file(stored.pk, {"comment": "Чужой"}, client=guest)

        self.assertEqual(response.status_code, 403)
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")

    def test_patch_requires_csrf_token(self):
        stored = self.edit_target()
        response = self.client.patch(
            reverse("file-detail", args=[stored.pk]), {"comment": "Изменённый"}, format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["detail"])
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")

    def test_patch_returns_json_for_missing_file(self):
        response = self.patch_file(uuid4(), {"comment": "Отсутствует"})

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.json()["detail"])

    def test_invalid_patch_never_applies_the_other_field(self):
        stored = self.edit_target()
        before = StoredFile.objects.values().get(pk=stored.pk)
        for changes in (
            {"original_name": "../report.pdf", "comment": "Допустимый комментарий"},
            {"original_name": "Допустимое имя.pdf", "comment": ["Неверный тип"]},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.patch_file(stored.pk, changes).status_code, 400)
                self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)

    def test_download_streams_owner_file_without_csrf(self):
        stored = self.edit_target()
        del self.client.cookies[settings.CSRF_COOKIE_NAME]

        response = self.download(stored.pk, HTTP_ACCEPT="application/pdf")

        self.assertEqual(self.read_download(response), b"report contents")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="report.pdf"')
        self.assertEqual(response["Content-Length"], str(stored.size_bytes))
        self.assertIn("no-store", response["Cache-Control"])

    def test_download_uses_current_unicode_name_after_rename(self):
        stored = self.edit_target()
        name = 'Отчёт за ноябрь + № 7 "финал".txt'
        self.assertEqual(self.patch_file(stored.pk, {"original_name": name}).status_code, 200)

        response = self.download(stored.pk)

        self.assertEqual(self.read_download(response), b"report contents")
        disposition, encoded_name = response["Content-Disposition"].split("; filename*=utf-8''")
        self.assertEqual(disposition, "attachment")
        self.assertEqual(unquote(encoded_name), name)
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_download_escapes_ascii_quotes_in_filename(self):
        stored = self.edit_target()
        self.assertEqual(
            self.patch_file(stored.pk, {"original_name": 'Report "final".pdf'}).status_code, 200
        )
        response = self.download(stored.pk)

        self.assertEqual(self.read_download(response), b"report contents")
        self.assertEqual(response["Content-Disposition"], r'attachment; filename="Report \"final\".pdf"')

    def test_download_streams_empty_and_binary_files(self):
        for content in (b"", bytes(range(256)) * 40):
            with self.subTest(size=len(content)):
                uploaded = self.upload(file=SimpleUploadedFile("data.my-cloud-unknown", content))
                self.assertEqual(uploaded.status_code, 201)

                response = self.download(uploaded.json()["id"])

                self.assertEqual(self.read_download(response), content)
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertEqual(response["Content-Length"], str(len(content)))

    def test_download_updates_only_timestamp_before_stream_is_consumed(self):
        stored = self.edit_target()
        before = StoredFile.objects.values().get(pk=stored.pk)
        path = stored.storage_path
        first_time = timezone.now()
        second_time = first_time + timedelta(seconds=1)

        for download_time in (first_time, second_time):
            with patch("files.views.timezone.now", return_value=download_time):
                response = self.download(stored.pk)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                StoredFile.objects.values().get(pk=stored.pk),
                {**before, "last_downloaded_at": download_time},
            )
            self.assertEqual(self.read_download(response), b"report contents")

        stored.refresh_from_db()
        self.assertEqual(stored.storage_path, path)
        self.assertEqual(path.read_bytes(), b"report contents")
        listed = self.client.get(self.url).json()[0]
        self.assertEqual(parse_datetime(listed["last_downloaded_at"]), second_time)

    def test_download_hides_foreign_file_even_from_staff(self):
        stored = self.edit_target()
        other_client = self.signed_in_client(self.other)
        missing = self.download(uuid4(), client=other_client)
        for is_staff in (False, True):
            with self.subTest(is_staff=is_staff):
                get_user_model().objects.filter(pk=self.other.pk).update(is_staff=is_staff)
                response = self.download(stored.pk, client=other_client)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), missing.json())
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_guest_cannot_download(self):
        stored = self.edit_target()
        guest = APIClient(enforce_csrf_checks=True)
        response = self.download(stored.pk, client=guest)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["detail"])
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_download_rejects_missing_and_invalid_uuid(self):
        missing = self.download(uuid4())
        invalid = self.client.get("/api/files/not-a-uuid/download/")

        self.assertEqual(missing.status_code, 404)
        self.assertTrue(missing.json()["detail"])
        self.assertEqual(invalid.status_code, 404)

    def test_missing_physical_file_preserves_download_timestamp_and_hides_paths(self):
        stored = self.edit_target()
        earlier = timezone.now() - timedelta(days=1)
        token = token_urlsafe(32)
        StoredFile.objects.filter(pk=stored.pk).update(last_downloaded_at=earlier, share_token=token)
        path = stored.storage_path
        path.unlink()

        response = self.download(stored.pk)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Файл недоступен."})
        for value in (str(path), stored.relative_path, token):
            self.assertNotIn(value, response.content.decode())
            self.assertNotIn(value, str(response.headers))
        stored.refresh_from_db()
        self.assertEqual(stored.last_downloaded_at, earlier)

    def test_download_does_not_expose_path_or_token_in_success_headers(self):
        stored = self.edit_target()
        token = token_urlsafe(32)
        StoredFile.objects.filter(pk=stored.pk).update(share_token=token)
        response = self.download(stored.pk)

        self.assertEqual(self.read_download(response), b"report contents")
        for value in (str(stored.storage_path), stored.relative_path, token):
            self.assertNotIn(value, str(response.headers))

    def test_download_rejects_file_and_owner_directory_symlinks(self):
        stored = self.edit_target()
        path = stored.storage_path
        outside = self.root.parent / "outside"
        outside.mkdir()
        external_file = outside / path.name
        external_file.write_bytes(b"outside storage")
        path.unlink()

        path.symlink_to(external_file)
        self.assertEqual(self.download(stored.pk).status_code, 404)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.download(stored.pk).status_code, 404)
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_download_does_not_follow_a_symlink_after_path_validation(self):
        stored = self.edit_target()
        path = stored.storage_path
        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"outside storage")
        path.unlink()
        path.symlink_to(outside)

        with patch.object(StoredFile, "storage_path", new_callable=PropertyMock, return_value=path):
            response = self.download(stored.pk)

        self.assertEqual(response.status_code, 404)
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_download_rejects_corrupted_path_in_database(self):
        stored = self.edit_target()
        StoredFile.objects.filter(pk=stored.pk).update(
            relative_path=f"{self.other.storage_relpath}/{stored.pk.hex}"
        )

        response = self.download(stored.pk)

        self.assertEqual(response.status_code, 404)
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_download_closes_file_if_timestamp_cannot_be_saved(self):
        stored = self.edit_target()
        with stored.storage_path.open("rb") as source:
            with (
                patch("files.views.open_download", return_value=source),
                patch("django.db.models.query.QuerySet.update", side_effect=DatabaseError("test failure")),
            ):
                response = self.download(stored.pk)
            self.assertTrue(source.closed)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Не удалось подготовить скачивание."})
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_delete_removes_only_target_record_and_file(self):
        target = self.edit_target()
        neighbor = self.edit_target()
        other_upload = self.upload(b"other owner", client=self.signed_in_client(self.other))
        self.assertEqual(other_upload.status_code, 201)
        foreign = StoredFile.objects.get(pk=other_upload.json()["id"])
        target_path = target.storage_path
        neighbor_path = neighbor.storage_path
        foreign_path = foreign.storage_path

        response = self.delete_file(target.pk)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(StoredFile.objects.filter(pk=target.pk).exists())
        self.assertFalse(target_path.exists())
        self.assertTrue(self.root.is_dir())
        self.assertEqual(neighbor_path.read_bytes(), b"report contents")
        self.assertEqual(foreign_path.read_bytes(), b"other owner")
        self.assertEqual(set(StoredFile.objects.values_list("pk", flat=True)), {neighbor.pk, foreign.pk})
        self.assertEqual([item["id"] for item in self.client.get(self.url).json()], [str(neighbor.pk)])
        self.assertEqual(self.delete_file(target.pk).status_code, 404)

    def test_delete_returns_404_for_missing_and_invalid_uuid(self):
        missing = self.delete_file(uuid4())
        invalid = self.client.delete("/api/files/not-a-uuid/", **self.csrf_header(self.client))

        self.assertEqual(missing.status_code, 404)
        self.assertTrue(missing.json()["detail"])
        self.assertEqual(invalid.status_code, 404)

    def test_delete_hides_foreign_files_even_from_staff(self):
        stored = self.edit_target()
        other_client = self.signed_in_client(self.other)
        missing = self.delete_file(uuid4(), client=other_client)
        for is_staff in (False, True):
            with self.subTest(is_staff=is_staff):
                get_user_model().objects.filter(pk=self.other.pk).update(is_staff=is_staff)
                response = self.delete_file(stored.pk, client=other_client)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), missing.json())
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(stored.storage_path.read_bytes(), b"report contents")

    def test_guest_cannot_delete_even_with_csrf_cookie(self):
        stored = self.edit_target()
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))

        self.assertEqual(self.delete_file(stored.pk, client=guest).status_code, 403)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertTrue(stored.storage_path.is_file())

    def test_delete_requires_valid_csrf(self):
        stored = self.edit_target()
        url = reverse("file-detail", args=[stored.pk])
        for headers in ({}, {**self.csrf_header(self.client), "HTTP_ORIGIN": "https://example.invalid"}):
            self.assertEqual(self.client.delete(url, **headers).status_code, 403)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertTrue(stored.storage_path.is_file())

    def test_delete_rejects_client_supplied_path(self):
        stored = self.edit_target()
        neighbor = self.edit_target()
        response = self.delete_file(stored.pk, data={"relative_path": neighbor.relative_path})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 2)
        self.assertTrue(stored.storage_path.is_file())
        self.assertTrue(neighbor.storage_path.is_file())

    def test_database_delete_failure_preserves_record_and_file(self):
        stored = self.edit_target()
        with (
            patch.object(StoredFile, "delete", side_effect=DatabaseError("test failure")),
            self.assertLogs("files.storage", level="ERROR"),
        ):
            response = self.delete_file(stored.pk)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(stored.storage_path.read_bytes(), b"report contents")

    def test_database_commit_failure_preserves_file_after_delete_sql(self):
        stored = self.edit_target()
        path = stored.storage_path
        original_commit = connection.savepoint_commit

        def fail_once(savepoint):
            self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
            self.assertTrue(path.is_file())
            commit.side_effect = original_commit
            raise DatabaseError("test failure when finishing transaction")

        with patch.object(connection, "savepoint_commit", side_effect=fail_once) as commit:
            response = self.delete_file(stored.pk)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(path.read_bytes(), b"report contents")

    def test_inaccessible_directory_preserves_record_and_file(self):
        stored = self.edit_target()
        path = stored.storage_path
        with patch("files.storage._open_owner_directory", side_effect=PermissionError(str(path))):
            response = self.delete_file(stored.pk)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(path.read_bytes(), b"report contents")

    def test_unlink_failure_after_commit_reports_orphan_without_leaking_paths(self):
        stored = self.edit_target()
        token = token_urlsafe(32)
        StoredFile.objects.filter(pk=stored.pk).update(share_token=token)
        path = stored.storage_path
        with (
            patch("files.storage.os.unlink", side_effect=PermissionError(str(path))),
            self.assertLogs("files.storage", level="ERROR") as logs,
        ):
            response = self.delete_file(stored.pk)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Не удалось удалить файл."})
        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(path.read_bytes(), b"report contents")
        self.assertIn(str(stored.pk), logs.output[0])
        for value in (str(path), stored.relative_path, token):
            self.assertNotIn(value, response.content.decode())
            self.assertNotIn(value, "\n".join(logs.output))

    def test_delete_rejects_symlink_outside_storage(self):
        stored = self.edit_target()
        path = stored.storage_path
        outside = self.root.parent / "outside"
        outside.mkdir()
        external_file = outside / path.name
        external_file.write_bytes(b"outside storage")
        path.unlink()
        path.symlink_to(external_file)
        self.assertEqual(self.delete_file(stored.pk).status_code, 500)
        self.assertTrue(path.is_symlink())

        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.delete_file(stored.pk).status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(external_file.read_bytes(), b"outside storage")

    def test_delete_rejects_path_from_another_owner_directory(self):
        stored = self.edit_target()
        path = stored.storage_path
        foreign_path = self.root / self.other.storage_relpath / stored.pk.hex
        foreign_path.parent.mkdir()
        foreign_path.write_bytes(b"outside owner directory")
        StoredFile.objects.filter(pk=stored.pk).update(
            relative_path=f"{self.other.storage_relpath}/{stored.pk.hex}"
        )

        self.assertEqual(self.delete_file(stored.pk).status_code, 500)
        self.assertTrue(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(path.read_bytes(), b"report contents")
        self.assertEqual(foreign_path.read_bytes(), b"outside owner directory")

    def test_delete_succeeds_when_physical_file_is_already_missing(self):
        stored = self.edit_target()
        path = stored.storage_path
        path.unlink()
        path.parent.rmdir()

        response = self.delete_file(stored.pk)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(self.client.get(self.url).json(), [])
        self.assertTrue(self.root.is_dir())

    def test_deleting_user_cascades_metadata_only(self):
        stored = self.edit_target()
        path = stored.storage_path

        self.owner.delete()

        self.assertFalse(StoredFile.objects.filter(pk=stored.pk).exists())
        self.assertEqual(path.read_bytes(), b"report contents")

    def test_owner_gets_stable_configured_url_with_opaque_token(self):
        stored = self.edit_target()
        self.assertEqual(self.patch_file(stored.pk, {"original_name": "Private Report.pdf"}).status_code, 200)
        response = self.share(stored.pk, HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        stored.refresh_from_db()
        self.assertRegex(stored.share_token, r"\A[A-Za-z0-9_-]{43}\Z")
        url = response.json()["url"]
        self.assertEqual(url, f"https://cloud.example.com/api/shared/{stored.share_token}/download/")
        for value in (stored.owner.username, stored.owner.full_name, stored.original_name, stored.relative_path):
            self.assertNotIn(value, unquote(url))
        self.assertEqual(self.share(stored.pk).json(), response.json())
        listed = self.client.get(self.url)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("share_token", listed.json()[0])
        self.assertNotIn(stored.share_token, listed.content.decode())

    def test_token_collision_is_retried_without_changing_existing_link(self):
        first = self.edit_target()
        second = self.edit_target()
        self.assertEqual(self.share(first.pk).status_code, 200)
        first.refresh_from_db()
        fresh_token = token_urlsafe(32)
        with patch("files.storage.token_urlsafe", side_effect=[first.share_token, fresh_token]) as generate:
            response = self.share(second.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate.call_count, 2)
        second.refresh_from_db()
        self.assertEqual(second.share_token, fresh_token)
        self.assertNotEqual(first.share_token, second.share_token)
        self.assertEqual(StoredFile.objects.get(pk=first.pk).share_token, first.share_token)

    def test_share_requires_owner_session_and_csrf(self):
        stored = self.edit_target()
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))
        self.assertEqual(self.share(stored.pk, client=guest).status_code, 403)
        self.assertEqual(self.client.post(reverse("file-share", args=[stored.pk])).status_code, 403)
        other = self.signed_in_client(self.other)
        foreign = self.share(stored.pk, client=other)
        missing = self.share(uuid4(), client=other)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())
        stored.refresh_from_db()
        self.assertIsNone(stored.share_token)

    def test_invalid_public_origin_does_not_create_token_or_use_request_host(self):
        stored = self.edit_target()
        url = reverse("file-share", args=[stored.pk])
        for origin in ("", "https://cloud.example.com/path", "https://cloud.example.com:99999"):
            with self.settings(PUBLIC_APP_ORIGIN=origin):
                response = self.client.post(url, HTTP_HOST="localhost", **self.csrf_header(self.client))
                self.assertEqual(response.status_code, 503)
        stored.refresh_from_db()
        self.assertIsNone(stored.share_token)

    def test_guest_streams_binary_and_empty_files_with_original_name(self):
        guest = APIClient(enforce_csrf_checks=True)
        name = 'Отчёт № 12 "версия".unknown-step12'
        for content in (b"", bytes(range(256)) * 40):
            uploaded = self.upload(file=SimpleUploadedFile(name, content))
            self.assertEqual(uploaded.status_code, 201)
            file_id = uploaded.json()["id"]
            share = self.share(file_id)
            self.assertEqual(share.status_code, 200)
            path = urlsplit(share.json()["url"]).path
            response = guest.get(path, HTTP_ACCEPT="application/octet-stream")

            self.assertEqual(self.read_download(response), content)
            self.assertEqual(response["Content-Type"], "application/octet-stream")
            disposition, encoded = response["Content-Disposition"].split("; filename*=utf-8''")
            self.assertEqual(disposition, "attachment")
            self.assertEqual(unquote(encoded), name)
            self.assertFalse(guest.cookies)
            stored = StoredFile.objects.get(pk=file_id)
            self.assertIsNotNone(stored.last_downloaded_at)
            for value in (stored.share_token, stored.owner.storage_relpath, stored.owner.username):
                self.assertNotIn(value, str(response.headers))

    def test_invalid_unknown_missing_and_deleted_links_return_same_404(self):
        guest = APIClient(enforce_csrf_checks=True)
        invalid = guest.get(reverse("shared-download", args=["invalid!"]))
        unknown = guest.get(reverse("shared-download", args=[token_urlsafe(32)]))
        self.assertEqual(invalid.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(invalid.json(), unknown.json())
        stored = self.edit_target()
        path = urlsplit(self.share(stored.pk).json()["url"]).path
        stored.storage_path.unlink()
        missing = guest.get(path)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), unknown.json())
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)
        self.assertEqual(self.delete_file(stored.pk).status_code, 204)
        deleted = guest.get(path)
        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(deleted.json(), unknown.json())

    def test_shared_download_rejects_symlink_outside_storage(self):
        stored = self.edit_target()
        shared_path = urlsplit(self.share(stored.pk).json()["url"]).path
        path = stored.storage_path
        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"outside storage")
        path.unlink()
        path.symlink_to(outside)

        response = APIClient().get(shared_path)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(outside), response.content.decode())
        stored.refresh_from_db()
        self.assertIsNone(stored.last_downloaded_at)

    def test_shared_paths_are_redacted_in_console_logs(self):
        console = next(
            handler for handler in logging.getLogger().handlers
            if any(isinstance(item, RedactShareTokens) for item in handler.filters)
        )
        path = reverse("shared-download", args=[token_urlsafe(32)])
        stream = StringIO()
        previous = console.setStream(stream)
        try:
            self.assertEqual(APIClient().get(path).status_code, 404)
            logging.getLogger("django.server").warning('"GET %s HTTP/1.1" 200 10', path)
        finally:
            console.setStream(previous)
        logs = stream.getvalue()
        self.assertNotIn(path.split("/")[3], logs)
        self.assertIn("Not Found: /api/shared/[redacted]/download/", logs)
        self.assertIn('"GET /api/shared/[redacted]/download/ HTTP/1.1" 200', logs)

    def test_all_file_operations_reject_logged_out_session(self):
        stored = self.edit_target()
        path = stored.storage_path
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.client.post(reverse("logout"), **self.csrf_header(self.client))
        self.assertEqual(response.status_code, 200)
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session_cookie

        responses = (
            self.client.get(self.url), self.upload(), self.download(stored.pk),
            self.patch_file(stored.pk, {"comment": "Изменить"}),
            self.delete_file(stored.pk), self.share(stored.pk),
        )
        for response in responses:
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        stored.refresh_from_db()
        self.assertEqual(stored.comment, "Исходный комментарий")
        self.assertIsNone(stored.share_token)
        self.assertEqual(path.read_bytes(), b"report contents")
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_all_file_mutations_reject_invalid_csrf_and_foreign_origin(self):
        stored = self.edit_target()
        before = StoredFile.objects.values().get(pk=stored.pk)
        for headers in (
            {}, {"HTTP_X_CSRFTOKEN": "z" * 32},
            {**self.csrf_header(self.client), "HTTP_ORIGIN": "https://example.invalid"},
        ):
            for method, url, data, format in (
                ("post", self.url, {"file": SimpleUploadedFile("extra.txt", b"extra")}, "multipart"),
                ("patch", reverse("file-detail", args=[stored.pk]), {"comment": "Изменить"}, "json"),
                ("delete", reverse("file-detail", args=[stored.pk]), {}, "json"),
                ("post", reverse("file-share", args=[stored.pk]), {}, "json"),
            ):
                with self.subTest(method=method, url=url, headers=headers):
                    response = getattr(self.client, method)(url, data, format=format, **headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertTrue(response.json()["detail"])
        self.assertEqual(StoredFile.objects.values().get(pk=stored.pk), before)
        self.assertEqual(self.saved_paths(), [self.root / stored.relative_path])

    def test_private_storage_is_not_served_at_direct_urls(self):
        stored = self.edit_target()
        guest = APIClient()
        for prefix in ("/media/", "/private_storage/", "/static/", "/api/files/"):
            for client in (self.client, guest):
                response = client.get(prefix + stored.relative_path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"report contents", response.content)
        self.assertTrue(stored.storage_path.is_file())

    def test_upload_rejects_directory_replacement_after_path_validation(self):
        self.root.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        owner_directory = self.root.resolve() / self.owner.storage_relpath
        original_mkdir = Path.mkdir

        def replace_owner_directory(directory, *args, **kwargs):
            original_mkdir(directory, *args, **kwargs)
            if directory == owner_directory:
                directory.rmdir()
                directory.symlink_to(outside, target_is_directory=True)

        with patch.object(Path, "mkdir", replace_owner_directory):
            response = self.upload()

        self.assertEqual(response.status_code, 500)
        self.assertFalse(StoredFile.objects.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def registered_client(self, username):
        client = APIClient(enforce_csrf_checks=True)
        client.get(reverse("csrf"))
        response = client.post(
            reverse("register"), {
                "username": username, "password": self.password,
                "full_name": "Тестовый пользователь", "email": f"{username}@example.invalid",
            }, format="json", **self.csrf_header(client),
        )
        self.assertEqual(response.status_code, 201)
        return self.signed_in_client(get_user_model().objects.get(pk=response.json()["id"]))

    def test_registered_user_full_private_file_lifecycle(self):
        client = self.registered_client("LifecycleA")
        uploaded = self.upload(b"lifecycle bytes", client=client)
        self.assertEqual(uploaded.status_code, 201)
        stored = StoredFile.objects.get(pk=uploaded.json()["id"])
        path = stored.storage_path
        renamed = self.patch_file(stored.pk, {"original_name": "Новый отчёт.pdf"}, client=client)
        self.assertEqual(renamed.status_code, 200)
        response = self.download(stored.pk, client=client)
        self.assertEqual(self.read_download(response), b"lifecycle bytes")
        self.assertEqual(unquote(response["Content-Disposition"].split("filename*=utf-8''")[1]), "Новый отчёт.pdf")
        self.assertEqual(self.delete_file(stored.pk, client=client).status_code, 204)
        self.assertEqual(client.get(self.url).json(), [])
        self.assertFalse(path.exists())

    def test_registered_user_public_link_lifecycle(self):
        client = self.registered_client("LifecycleB")
        uploaded = self.upload(b"public lifecycle", client=client)
        self.assertEqual(uploaded.status_code, 201)
        stored = StoredFile.objects.get(pk=uploaded.json()["id"])
        path = stored.storage_path
        response = self.share(stored.pk, client=client)
        self.assertEqual(response.status_code, 200)
        shared_path = urlsplit(response.json()["url"]).path
        guest = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.read_download(guest.get(shared_path)), b"public lifecycle")
        self.assertFalse(guest.cookies)
        self.assertEqual(self.delete_file(stored.pk, client=client).status_code, 204)
        self.assertEqual(guest.get(shared_path).status_code, 404)
        self.assertFalse(path.exists())

    def test_two_registered_users_remain_isolated_through_all_file_operations(self):
        first = self.registered_client("IsolationA")
        second = self.registered_client("IsolationB")
        first_upload = self.upload(b"first owner", client=first)
        second_upload = self.upload(b"second owner", client=second)
        self.assertEqual(first_upload.status_code, 201)
        self.assertEqual(second_upload.status_code, 201)
        first_file = StoredFile.objects.get(pk=first_upload.json()["id"])
        second_file = StoredFile.objects.get(pk=second_upload.json()["id"])
        self.assertNotEqual(first_file.relative_path, second_file.relative_path)
        for client, own, foreign, content in (
            (first, first_file, second_file, b"first owner"),
            (second, second_file, first_file, b"second owner"),
        ):
            listed = client.get(self.url, {"owner": foreign.owner_id, "owner_id": foreign.owner_id})
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["id"] for item in listed.json()], [str(own.pk)])
            for target in (foreign.pk, uuid4()):
                self.assertEqual(self.download(target, client=client).status_code, 404)
                self.assertEqual(self.patch_file(target, {"comment": "Чужой"}, client=client).status_code, 404)
                self.assertEqual(self.delete_file(target, client=client).status_code, 404)
                self.assertEqual(self.share(target, client=client).status_code, 404)
            self.assertEqual(self.read_download(self.download(own.pk, client=client)), content)
        self.assertEqual(first_file.storage_path.read_bytes(), b"first owner")
        self.assertEqual(second_file.storage_path.read_bytes(), b"second owner")
