from django.conf import settings
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase

from .models import User
from .serializers import RegistrationSerializer


class RegistrationTests(APITestCase):
    def setUp(self):
        self.url = reverse("register")
        self.payload = {
            "username": "Alice7",
            "full_name": "Алиса Иванова",
            "email": "alice@example.com",
            "password": "Example7!",
        }
        self.client = APIClient(enforce_csrf_checks=True)
        request = HttpRequest()
        self.csrf_token = get_token(request)
        self.client.cookies[settings.CSRF_COOKIE_NAME] = request.META["CSRF_COOKIE"]

    def register(self, payload, **headers):
        return self.client.post(
            self.url, payload, format="json", HTTP_X_CSRFTOKEN=self.csrf_token, **headers
        )

    def assert_invalid(self, field, value):
        response = self.register({**self.payload, field: value})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()[field])
        self.assertFalse(User.objects.filter(email=self.payload["email"]).exists())

    def test_guest_can_register_with_valid_csrf(self):
        response = self.register(self.payload, HTTP_ORIGIN="http://testserver")

        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username=self.payload["username"])
        self.assertEqual(response.json(), {
            "id": user.pk,
            "username": self.payload["username"],
            "full_name": self.payload["full_name"],
            "email": self.payload["email"],
        })
        self.assertNotEqual(user.password, self.payload["password"])
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.storage_relpath)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_rejects_username_outside_length_limits(self):
        for username in ("abc", "a" * 21):
            with self.subTest(username=username):
                self.assert_invalid("username", username)

    def test_rejects_invalid_username_format(self):
        for username in ("1abc", "a_bc", "Аlice7", " Alice7", "Alice7 "):
            with self.subTest(username=username):
                self.assert_invalid("username", username)

    def test_rejects_invalid_email(self):
        self.assert_invalid("email", "invalid-email")

    def test_rejects_empty_full_name(self):
        for name in ("", "  \t "):
            with self.subTest(name=name):
                self.assert_invalid("full_name", name)

    def test_rejects_each_invalid_password(self):
        for case, password in {
            "short": "Ab1!x",
            "no_uppercase": "example7!",
            "no_digit": "Example!",
            "no_special": "Example7",
        }.items():
            with self.subTest(case=case):
                self.assert_invalid("password", password)

    def test_rejects_existing_username(self):
        self.assertEqual(self.register(self.payload).status_code, 201)

        response = self.register(self.payload)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["username"])
        self.assertEqual(User.objects.filter(username=self.payload["username"]).count(), 1)

    def test_handles_duplicate_created_after_validation(self):
        serializer = RegistrationSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        User.objects.create_user(**self.payload)

        with self.assertRaises(ValidationError) as error:
            serializer.save()

        self.assertIn("username", error.exception.detail)
        self.assertEqual(User.objects.filter(username=self.payload["username"]).count(), 1)

    def test_reports_each_missing_field(self):
        for field in self.payload:
            with self.subTest(field=field):
                payload = self.payload.copy()
                del payload[field]
                response = self.register(payload)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()[field])
        self.assertFalse(User.objects.filter(username=self.payload["username"]).exists())

    def test_rejects_system_fields(self):
        system_fields = {
            "id": 100,
            "is_staff": True,
            "is_superuser": True,
            "is_active": False,
            "storage_relpath": "custom-path",
            "groups": [],
            "user_permissions": [],
        }
        response = self.register({**self.payload, **system_fields})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json().keys(), system_fields.keys())
        self.assertFalse(User.objects.filter(username=self.payload["username"]).exists())

    def test_rejects_missing_or_invalid_csrf(self):
        for headers in ({}, {"HTTP_X_CSRFTOKEN": "z" * 32}):
            with self.subTest(headers=headers):
                response = self.client.post(self.url, self.payload, format="json", **headers)
                self.assertEqual(response.status_code, 403)
                self.assertIn("detail", response.json())

        del self.client.cookies[settings.CSRF_COOKIE_NAME]
        self.assertEqual(self.register(self.payload).status_code, 403)
        self.assertFalse(User.objects.filter(username=self.payload["username"]).exists())

    def test_rejects_cross_origin_with_valid_csrf(self):
        response = self.register(self.payload, HTTP_ORIGIN="https://example.invalid")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username=self.payload["username"]).exists())
