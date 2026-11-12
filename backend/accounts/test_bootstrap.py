import os
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .models import User


class AdminBootstrapTests(TestCase):
    def test_seeded_admin_stays_inactive_without_valid_password(self):
        for password in ("", "weak"):
            with self.subTest(password_configured=bool(password)):
                with patch.dict(os.environ, MY_CLOUD_ADMIN_PASSWORD=password), self.assertRaises(CommandError):
                    call_command("bootstrap_admin", stdout=StringIO())

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_staff)
        self.assertFalse(admin.is_active)
        self.assertFalse(admin.has_usable_password())
        self.assertIsNone(authenticate(username="admin", password="TestAdmin7!"))

    def test_bootstrap_enables_admin_once(self):
        password = "TestAdmin7!"
        with patch.dict(os.environ, MY_CLOUD_ADMIN_PASSWORD=password):
            call_command("bootstrap_admin", stdout=StringIO())

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_active)
        self.assertTrue(admin.is_staff)
        self.assertEqual(authenticate(username="admin", password=password).pk, admin.pk)
        stored_hash = admin.password

        with patch.dict(os.environ, MY_CLOUD_ADMIN_PASSWORD="AnotherTest8!"):
            call_command("bootstrap_admin", stdout=StringIO())

        admin.refresh_from_db()
        self.assertEqual(admin.password, stored_hash)
