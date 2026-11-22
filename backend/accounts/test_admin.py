from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from files.models import StoredFile
from files.storage import save_upload

from .models import User


class AdminUserTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Example7!"
        cls.admin = User.objects.create_user(
            username="Admin7", full_name="Администратор", email="admin7@example.invalid",
            password=cls.password, is_staff=True,
        )
        cls.member = User.objects.create_user(
            username="Member7", full_name="Пользователь", email="member7@example.invalid",
            password=cls.password,
        )

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "private"
        self.enterContext(self.settings(FILE_STORAGE_ROOT=self.root))
        self.url = reverse("admin-users")
        self.client = self.sign_in(self.admin)

    def sign_in(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get(reverse("csrf"))
        response = client.post(reverse("login"), {
            "username": user.username, "password": self.password,
        }, format="json", **self.csrf(client))
        self.assertEqual(response.status_code, 200)
        return client

    def csrf(self, client):
        return {"HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}

    def change_role(self, user=None, data=None, client=None, **headers):
        client = self.client if client is None else client
        user = self.member if user is None else user
        return client.patch(
            reverse("admin-user-detail", args=[user.pk]),
            {"is_staff": True} if data is None else data,
            format="json", **{**self.csrf(client), **headers},
        )

    def file_record(self, owner, size):
        return StoredFile.objects.create(owner=owner, original_name="Отчёт.pdf", size_bytes=size)

    def test_admin_list_has_public_fields_and_username_order(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([row["username"] for row in rows], sorted(User.objects.values_list("username", flat=True)))
        self.assertIn("no-store", response["Cache-Control"])
        expected = {"id", "username", "full_name", "email", "is_staff", "files_count", "total_size_bytes"}
        self.assertTrue(all(set(row) == expected for row in rows))
        member = next(row for row in rows if row["id"] == self.member.pk)
        self.assertEqual(member, {
            "id": self.member.pk, "username": self.member.username,
            "full_name": self.member.full_name, "email": self.member.email,
            "is_staff": False, "files_count": 0, "total_size_bytes": 0,
        })

    def test_guest_cannot_list_or_patch(self):
        guest = APIClient(enforce_csrf_checks=True)
        guest.get(reverse("csrf"))
        for response in (guest.get(self.url), self.change_role(client=guest)):
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])

    def test_member_cannot_list_or_patch_even_their_own_role(self):
        member = self.sign_in(self.member)
        for response in (member.get(self.url), self.change_role(client=member), self.change_role(self.admin, client=member)):
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)

    def test_totals_include_zero_and_large_sizes_for_separate_owners(self):
        for size in (0, 11, 11):
            self.file_record(self.member, size)
        self.file_record(self.admin, 2 ** 33)
        with patch.object(Path, "rglob", side_effect=AssertionError("Не сканировать диск")):
            rows = {row["id"]: row for row in self.client.get(self.url).json()}
        self.assertEqual((rows[self.member.pk]["files_count"], rows[self.member.pk]["total_size_bytes"]), (3, 22))
        self.assertEqual((rows[self.admin.pk]["files_count"], rows[self.admin.pk]["total_size_bytes"]), (1, 2 ** 33))
        seed = User.objects.get(username="admin")
        self.assertEqual((rows[seed.pk]["files_count"], rows[seed.pk]["total_size_bytes"]), (0, 0))

    def test_user_list_uses_one_aggregate_query_without_n_plus_one(self):
        for index in range(5):
            user = User.objects.create_user(username=f"Count{index}", full_name="Имя", email="count@example.invalid")
            self.file_record(user, index)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(self.url).status_code, 200)
        aggregate = [query["sql"] for query in captured if '"files_storedfile"' in query["sql"]]
        self.assertEqual(len(aggregate), 1)
        self.assertIn("COUNT(", aggregate[0])
        self.assertIn("SUM(", aggregate[0])
        self.assertEqual(len(captured), 3)  # Сессия, текущий пользователь, список с агрегатами.

    def test_totals_follow_metadata_edits_and_file_deletion(self):
        stored = save_upload(self.admin, SimpleUploadedFile("Отчёт.pdf", b"abcde"), "")
        before = next(row for row in self.client.get(self.url).json() if row["id"] == self.admin.pk)
        changed = self.client.patch(reverse("file-detail", args=[stored.pk]), {
            "original_name": "Новое имя.pdf", "comment": "Текст",
        }, format="json", **self.csrf(self.client))
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(next(row for row in self.client.get(self.url).json() if row["id"] == self.admin.pk), before)
        path = stored.storage_path
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(reverse("file-detail", args=[stored.pk]), **self.csrf(self.client))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(path.exists())
        row = next(row for row in self.client.get(self.url).json() if row["id"] == self.admin.pk)
        self.assertEqual((row["files_count"], row["total_size_bytes"]), (0, 0))

    def test_grant_role_returns_current_metadata_without_superuser_privileges(self):
        self.file_record(self.member, 17)
        before = User.objects.values().get(pk=self.member.pk)
        response = self.change_role()
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["is_staff"], True)
        self.assertEqual(response.json()["total_size_bytes"], 17)
        self.assertEqual(User.objects.values().get(pk=self.member.pk), {**before, "is_staff": True})

    def test_role_changes_apply_to_existing_session_immediately(self):
        member = self.sign_in(self.member)
        self.assertEqual(member.get(self.url).status_code, 403)
        self.assertEqual(self.change_role().status_code, 200)
        self.assertEqual(member.get(self.url).status_code, 200)
        self.assertTrue(member.get(reverse("me")).json()["is_staff"])
        response = self.change_role(data={"is_staff": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_staff"])
        self.assertEqual(member.get(self.url).status_code, 403)
        self.assertFalse(member.get(reverse("me")).json()["is_staff"])
        self.assertEqual(self.change_role(self.admin, client=member).status_code, 403)

    def test_admin_can_demote_self_when_another_active_admin_exists(self):
        self.assertEqual(self.change_role().status_code, 200)
        self.assertEqual(self.change_role(self.admin, {"is_staff": False}).status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_last_active_admin_cannot_be_demoted(self):
        response = self.change_role(self.admin, {"is_staff": False})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["is_staff"])
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)

    def test_inactive_or_unusable_admin_does_not_bypass_last_admin_guard(self):
        self.member.is_staff = True
        for active in (False, True):
            self.member.is_active = active
            if active:
                self.member.set_unusable_password()
            self.member.save()
            response = self.change_role(self.admin, {"is_staff": False})
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["is_staff"])

    def test_demoting_inactive_admin_or_repeating_role_is_allowed(self):
        seed = User.objects.get(username="admin")
        self.assertEqual(self.change_role(seed, {"is_staff": False}).status_code, 200)
        self.assertEqual(self.change_role(self.admin).status_code, 200)
        self.assertEqual(self.change_role(data={"is_staff": False}).status_code, 200)

    def test_patch_requires_valid_csrf_and_same_origin(self):
        url = reverse("admin-user-detail", args=[self.member.pk])
        for headers in ({}, {"HTTP_X_CSRFTOKEN": "z" * 32}, {**self.csrf(self.client), "HTTP_ORIGIN": "https://example.invalid"}):
            response = self.client.patch(url, {"is_staff": True}, format="json", **headers)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.json()["detail"])
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)

    def test_forbidden_fields_reject_entire_update(self):
        before = User.objects.values().get(pk=self.member.pk)
        for field, value in {
            "id": self.admin.pk, "username": "Changed7", "email": "new@example.invalid",
            "password": "Secret9!", "full_name": "Другое имя", "is_superuser": True,
            "is_active": False, "storage_relpath": "a" * 32, "groups": [],
            "user_permissions": [], "last_login": None, "unknown": "value",
        }.items():
            with self.subTest(field=field):
                response = self.change_role(data={"is_staff": True, field: value})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()[field])
                self.assertEqual(User.objects.values().get(pk=self.member.pk), before)

    def test_role_requires_a_json_boolean_and_cannot_be_omitted(self):
        for payload in ({}, {"is_staff": None}, {"is_staff": "true"}, {"is_staff": 1}, {"is_staff": []}, {"is_staff": {}}, []):
            response = self.change_role(data=payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response["Content-Type"], "application/json")
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)

    def test_missing_and_invalid_ids_return_json_404(self):
        for user_id in ("0", "999999", "bad-id", "-1", "9" * 50, "9223372036854775808"):
            response = self.client.patch(reverse("admin-user-detail", args=[user_id]), {"is_staff": True}, format="json", **self.csrf(self.client))
            self.assertEqual(response.status_code, 404)
            self.assertTrue(response.json()["detail"])

    def test_malformed_json_and_unsupported_media_are_safe(self):
        for body, content_type, status in (("{", "application/json", 400), ("is_staff=true", "text/plain", 415)):
            response = self.client.patch(reverse("admin-user-detail", args=[self.member.pk]), body, content_type=content_type, **self.csrf(self.client))
            self.assertEqual(response.status_code, status)
            self.assertTrue(response.json()["detail"])

    def test_only_staff_flag_grants_application_admin_access(self):
        self.member.is_superuser = True
        self.member.save(update_fields=["is_superuser"])
        member = self.sign_in(self.member)
        self.assertEqual(member.get(self.url).status_code, 403)
        self.assertFalse(self.admin.is_superuser)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_admin_api_does_not_implement_deletion_or_file_access(self):
        response = self.client.delete(reverse("admin-user-detail", args=[self.member.pk]), **self.csrf(self.client))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.member.pk).exists())
        stored = self.file_record(self.member, 10)
        response = self.client.get(reverse("file-download", args=[stored.pk]))
        self.assertEqual(response.status_code, 404)

    def test_secrets_are_absent_from_responses_and_logs(self):
        stored = self.file_record(self.member, 10)
        stored.share_token = "secret-share-token"
        stored.save(update_fields=["share_token"])
        with self.assertLogs("django", level="WARNING") as logs:
            responses = [self.client.get(self.url), self.change_role(), self.change_role(data={"is_staff": True, "password": "DoNotLog9!"})]
        output = "\n".join(logs.output) + "\n".join(response.content.decode() for response in responses)
        for secret in (self.password, self.member.password, self.member.storage_relpath, stored.relative_path, stored.share_token, "DoNotLog9!", self.client.cookies[settings.SESSION_COOKIE_NAME].value):
            self.assertNotIn(secret, output)
