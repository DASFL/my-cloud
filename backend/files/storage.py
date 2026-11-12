import logging

from django.db import transaction

from .models import StoredFile

logger = logging.getLogger(__name__)


def save_upload(owner, uploaded_file, comment):
    stored = StoredFile(
        owner=owner,
        original_name=uploaded_file.name,
        size_bytes=uploaded_file.size,
        comment=comment,
    )
    created_path = None
    committed = False
    try:
        with transaction.atomic(durable=True):
            stored.save(force_insert=True)
            path = stored.storage_path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as destination:
                created_path = path
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)
        committed = True
    finally:
        if not committed and created_path is not None:
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                logger.error("Не удалось очистить файл после сбоя загрузки.")
    return stored
