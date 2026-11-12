import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Активирует администратора admin и задаёт ему пароль из окружения."

    @transaction.atomic
    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            admin = user_model.objects.select_for_update().get(username="admin")
        except user_model.DoesNotExist:
            raise CommandError("Сначала примените миграции.") from None

        if not admin.is_staff:
            raise CommandError("У пользователя admin нет роли администратора.")
        if admin.has_usable_password():
            self.stdout.write("Пароль администратора уже установлен.")
            return

        password = os.environ.get("MY_CLOUD_ADMIN_PASSWORD")
        if not password:
            raise CommandError("Задайте MY_CLOUD_ADMIN_PASSWORD в окружении.")
        try:
            validate_password(password, user=admin)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from None

        admin.set_password(password)
        admin.is_active = True
        admin.save(update_fields=["password", "is_active"])
        self.stdout.write(self.style.SUCCESS("Администратор admin активирован."))
