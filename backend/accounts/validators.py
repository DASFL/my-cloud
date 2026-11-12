import re

from django.core.exceptions import ValidationError


class PasswordComplexityValidator:
    def validate(self, password, user=None):
        has_uppercase = re.search(r"[A-Z]", password)
        has_digit = re.search(r"[0-9]", password)
        has_special = any(
            char.isprintable() and not (char.isalnum() or char.isspace())
            for char in password
        )
        if not (has_uppercase and has_digit and has_special):
            raise ValidationError(self.get_help_text(), code="password_complexity")

    def get_help_text(self):
        return "В пароле нужны заглавная латинская буква, цифра и специальный символ."
