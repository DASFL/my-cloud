from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from .models import User


class AuthSessionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.credentials = {"username": "Alice7", "password": "Example7!"}
        cls.user = User.objects.create_user(
            **cls.credentials, full_name="Алиса Иванова", email="alice@example.com"
        )

    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)

    def post_with_csrf(self, name, data=None, **headers):
        cookie = self.client.cookies[settings.CSRF_COOKIE_NAME]
        return self.client.post(
            reverse(name), data or {}, format="json", HTTP_X_CSRFTOKEN=cookie.value, **headers
        )

    def sign_in(self, **headers):
        self.client.get(reverse("csrf"))
        return self.post_with_csrf("login", self.credentials, **headers)

    def test_csrf_endpoint_sets_cookie_without_session(self):
        response = self.client.get(reverse("csrf"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["csrf_token"])
        self.assertEqual(response.cookies[settings.CSRF_COOKIE_NAME]["samesite"], "Lax")
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertIn("no-store", response["Cache-Control"])

    def test_login_returns_public_user_and_creates_server_session(self):
        response = self.sign_in(HTTP_ORIGIN="http://testserver")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "id": self.user.pk,
            "username": self.user.username,
            "full_name": self.user.full_name,
            "email": self.user.email,
            "is_staff": False,
        })
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        session = Session.objects.get(session_key=cookie.value)
        self.assertEqual(session.get_decoded()[SESSION_KEY], str(self.user.pk))

    def test_login_uses_same_error_for_unknown_login_and_wrong_password(self):
        self.client.get(reverse("csrf"))
        expected_error = {"detail": "Неверный логин или пароль."}
        for credentials in (
            {**self.credentials, "username": "Nobody7"},
            {**self.credentials, "password": "WrongPass7!"},
        ):
            with self.subTest(username=credentials["username"]):
                response = self.post_with_csrf("login", credentials)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), expected_error)
                self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_login_reports_missing_fields(self):
        self.client.get(reverse("csrf"))
        for field in self.credentials:
            with self.subTest(field=field):
                payload = self.credentials.copy()
                del payload[field]
                response = self.post_with_csrf("login", payload)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()[field])

    def test_me_returns_user_from_existing_session(self):
        login_response = self.sign_in()
        self.assertEqual(login_response.status_code, 200)

        response = self.client.get(reverse("me"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), login_response.json())
        self.assertIn("no-store", response["Cache-Control"])

    def test_admin_role_comes_from_staff_flag(self):
        User.objects.filter(pk=self.user.pk).update(is_staff=True)

        response = self.sign_in()

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["is_staff"], True)
        self.assertIs(self.client.get(reverse("me")).json()["is_staff"], True)

    def test_protected_endpoints_reject_guest(self):
        self.client.get(reverse("csrf"))
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
        self.assertEqual(self.post_with_csrf("logout").status_code, 403)

    def test_logout_invalidates_session_even_if_cookie_is_reused(self):
        self.assertEqual(self.sign_in().status_code, 200)
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        response = self.post_with_csrf("logout", HTTP_ORIGIN="http://testserver")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"detail": "Вы вышли из аккаунта."})
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]["max-age"], 0)
        self.assertFalse(Session.objects.filter(session_key=session_cookie).exists())
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)

        self.client.cookies[settings.SESSION_COOKIE_NAME] = session_cookie
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, session_cookie)

    def test_get_logout_preserves_session(self):
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.get(reverse("me")).status_code, 200)

    def test_login_requires_csrf(self):
        self.client.get(reverse("csrf"))
        response = self.client.post(reverse("login"), self.credentials, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertIn("detail", response.json())
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_logout_requires_current_csrf_token(self):
        self.client.get(reverse("csrf"))
        old_token = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(self.post_with_csrf("login", self.credentials).status_code, 200)
        self.assertNotEqual(self.client.cookies[settings.CSRF_COOKIE_NAME].value, old_token)

        for headers in ({}, {"HTTP_X_CSRFTOKEN": old_token}):
            with self.subTest(headers=headers):
                response = self.client.post(reverse("logout"), **headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.client.get(reverse("me")).status_code, 200)

    def test_unsafe_auth_requests_reject_foreign_origin(self):
        origin = {"HTTP_ORIGIN": "https://example.invalid"}
        self.assertEqual(self.sign_in(**origin).status_code, 403)
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertEqual(self.post_with_csrf("logout", **origin).status_code, 403)
        self.assertEqual(self.client.get(reverse("me")).status_code, 200)

    def test_registration_login_me_and_logout_work_together(self):
        credentials = {"username": "NewUser7", "password": "Example8!"}
        self.client.get(reverse("csrf"))
        registered = self.post_with_csrf("register", {
            **credentials, "full_name": "Новый Пользователь", "email": "new@example.com"
        })
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
        self.assertEqual(self.post_with_csrf("login", credentials).status_code, 200)
        current_user = self.client.get(reverse("me"))
        self.assertEqual(current_user.status_code, 200)
        self.assertEqual(current_user.json(), {**registered.json(), "is_staff": False})
        self.assertEqual(self.post_with_csrf("logout").status_code, 200)
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
