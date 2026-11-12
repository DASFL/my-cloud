from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.db import models


class StoredFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stored_files"
    )
    original_name = models.CharField(max_length=255)
    relative_path = models.CharField(max_length=65, unique=True, editable=False)
    size_bytes = models.PositiveBigIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    share_token = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(original_name=""),
                name="files_storedfile_original_name_present",
            ),
            models.CheckConstraint(
                condition=models.Q(relative_path__regex=r"\A[0-9a-f]{32}/[0-9a-f]{32}\Z"),
                name="files_storedfile_relative_path_format",
            ),
        ]

    def _build_relative_path(self):
        return f"{self.owner.storage_relpath}/{UUID(str(self.id)).hex}"

    def save(self, *args, **kwargs):
        if self._state.adding and self.owner_id is not None:
            self.relative_path = self._build_relative_path()
        return super().save(*args, **kwargs)

    @property
    def storage_path(self):
        if self.relative_path != self._build_relative_path():
            raise ValueError("Путь файла не соответствует владельцу и идентификатору.")

        root = Path(settings.FILE_STORAGE_ROOT).resolve()
        path = root / self.relative_path
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved != path:
            raise ValueError("Путь файла должен вести в каталог владельца без символических ссылок.")
        return path
