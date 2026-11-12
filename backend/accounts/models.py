from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models

USERNAME_PATTERN = r"\A[A-Za-z][A-Za-z0-9]{3,19}\Z"
STORAGE_RELPATH_PATTERN = r"\A[0-9a-f]{32}\Z"


def generate_storage_relpath():
    return uuid4().hex


class User(AbstractUser):
    first_name = None
    last_name = None

    username = models.CharField(
        max_length=20,
        unique=True,
        validators=[
            RegexValidator(
                USERNAME_PATTERN,
                "Логин должен содержать от 4 до 20 латинских букв и цифр и начинаться с буквы.",
            ),
        ],
    )
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    storage_relpath = models.CharField(
        max_length=32, default=generate_storage_relpath, editable=False, unique=True
    )

    REQUIRED_FIELDS = ["full_name", "email"]

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(username__regex=USERNAME_PATTERN),
                name="accounts_user_username_format",
            ),
            models.CheckConstraint(
                condition=~models.Q(full_name="") & ~models.Q(email=""),
                name="accounts_user_required_fields",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_relpath__regex=STORAGE_RELPATH_PATTERN),
                name="accounts_user_storage_relpath_format",
            ),
        ]

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        names = self.full_name.split(maxsplit=1)
        return names[0] if names else self.username
