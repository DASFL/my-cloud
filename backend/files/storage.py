import logging
import os
import stat
from contextlib import ExitStack

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


def open_download(stored):
    path = stored.storage_path
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as directories:
        root_fd = os.open(path.parent.parent, directory_flags)
        directories.callback(os.close, root_fd)
        owner_fd = os.open(path.parent.name, directory_flags, dir_fd=root_fd)
        directories.callback(os.close, owner_fd)

        def open_in_owner_directory(name, flags):
            return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=owner_fd)

        source = open(path.name, "rb", opener=open_in_owner_directory)
        try:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("Скачивать можно только обычные файлы.")
        except BaseException:
            source.close()
            raise
        return source
