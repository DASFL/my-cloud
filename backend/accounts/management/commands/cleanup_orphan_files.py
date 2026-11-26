import os
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from files.storage import open_storage_directory, remove_storage_files, storage_file_names


class Command(BaseCommand):
    help = "Находит файлы в каталогах без владельца; удаление только с --delete."

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true", help="Удалить найденные файлы.")

    def handle(self, *args, **options):
        root = Path(settings.FILE_STORAGE_ROOT).resolve()
        if not root.exists():
            self.stdout.write("Хранилище отсутствует, оставшихся файлов нет.")
            return
        failed = False
        found = 0
        for directory in sorted(os.listdir(root)):
            if not re.fullmatch(r"[0-9a-f]{32}", directory):
                self.stderr.write("Пропущена неизвестная запись в корне хранилища.")
                continue
            if User.objects.filter(storage_relpath=directory).exists():
                continue
            try:
                with open_storage_directory(directory) as owner_fd:
                    names = storage_file_names(owner_fd)
                    if not names:
                        continue
                    if User.objects.filter(storage_relpath=directory).exists():
                        continue
                    found += len(names)
                    if options["delete"]:
                        remove_storage_files(owner_fd, names)
                    action = "Удалено" if options["delete"] else "Найдено"
                    self.stdout.write(f"{directory}: {action} файлов: {len(names)}")
            except (OSError, ValueError, RuntimeError):
                failed = True
                self.stderr.write(f"{directory}: небезопасная запись или ошибка доступа; проверьте вручную.")
        self.stdout.write(f"Всего файлов: {found}. Пустые каталоги и корень сохранены.")
        if not options["delete"]:
            self.stdout.write("Режим проверки: файлы не удалялись. Для очистки используйте --delete.")
        if failed:
            raise CommandError("Не все каталоги удалось безопасно обработать.")
