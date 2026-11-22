import logging
import os
import stat
from contextlib import ExitStack, contextmanager
from secrets import token_urlsafe

from django.db import DatabaseError, IntegrityError, transaction
from psycopg.errors import UniqueViolation

from .models import StoredFile

logger = logging.getLogger(__name__)


def save_upload(owner, uploaded_file, comment):
    stored = StoredFile(
        owner=owner,
        original_name=uploaded_file.name,
        size_bytes=uploaded_file.size,
        comment=comment,
    )
    created_name = None
    committed = False
    with ExitStack() as directories:
        try:
            with transaction.atomic(durable=True):
                stored.save(force_insert=True)
                path = stored.storage_path
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                owner_fd = directories.enter_context(_open_owner_directory(path))

                def open_in_owner_directory(name, flags):
                    return os.open(name, flags | os.O_NOFOLLOW, mode=0o600, dir_fd=owner_fd)

                with open(path.name, "xb", opener=open_in_owner_directory) as destination:
                    created_name = path.name
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)
            committed = True
        finally:
            if not committed and created_name is not None:
                try:
                    os.unlink(created_name, dir_fd=owner_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.error("Не удалось очистить файл после сбоя загрузки.")
    return stored


@contextmanager
def _open_owner_directory(path):
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as directories:
        root_fd = os.open(path.parent.parent, directory_flags)
        directories.callback(os.close, root_fd)
        owner_fd = os.open(path.parent.name, directory_flags, dir_fd=root_fd)
        directories.callback(os.close, owner_fd)
        yield owner_fd


def open_download(stored):
    path = stored.storage_path
    with _open_owner_directory(path) as owner_fd:
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


def delete_stored_file(owner, file_id):
    committed = False
    try:
        with ExitStack() as directories:
            with transaction.atomic(durable=True):
                stored = StoredFile.objects.select_for_update().get(owner=owner, pk=file_id)
                path = stored.storage_path
                try:
                    owner_fd = directories.enter_context(_open_owner_directory(path))
                    mode = os.stat(path.name, dir_fd=owner_fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    owner_fd = None
                else:
                    if not stat.S_ISREG(mode):
                        raise ValueError("Удалять можно только обычные файлы.")
                stored.delete()

            committed = True
            if owner_fd is not None:
                try:
                    os.unlink(path.name, dir_fd=owner_fd)
                except FileNotFoundError:
                    pass
    except (DatabaseError, OSError, ValueError, RuntimeError):
        if committed:
            logger.error("Запись файла %s удалена, но очистка хранилища завершилась ошибкой.", file_id)
        else:
            logger.error("Не удалось подтвердить удаление записи файла %s.", file_id)
        raise


def get_share_token(owned_files, file_id):
    with transaction.atomic():
        stored = owned_files.select_for_update().get(pk=file_id)
        if stored.share_token:
            return stored.share_token

        for _ in range(3):
            stored.share_token = token_urlsafe(32)
            try:
                with transaction.atomic():
                    stored.save(update_fields=["share_token"])
                return stored.share_token
            except IntegrityError as error:
                cause = error.__cause__
                if (
                    not isinstance(cause, UniqueViolation)
                    or cause.diag.constraint_name != "files_storedfile_share_token_key"
                ):
                    raise

        raise DatabaseError("Не удалось создать уникальный токен.")
