import logging
import re
from contextlib import ExitStack
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import DatabaseError
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StoredFile
from .serializers import FileUploadSerializer, StoredFileSerializer, StoredFileUpdateSerializer
from .storage import delete_stored_file, get_share_token, open_download, save_upload

logger = logging.getLogger(__name__)


def public_origin():
    origin = settings.PUBLIC_APP_ORIGIN
    URLValidator(schemes=["http", "https"])(origin)
    parts = urlsplit(origin)
    if (
        parts.port == 0 or parts.username is not None
        or parts.path not in ("", "/") or "?" in origin or "#" in origin
    ):
        raise ValueError("Ожидался публичный адрес приложения без пути и параметров.")
    return origin.removesuffix("/")


@method_decorator(never_cache, name="dispatch")
class FileListView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]
    renderer_classes = [JSONRenderer]

    def get(self, request):
        files = StoredFile.objects.filter(owner=request.user).order_by("-created_at", "-id")
        return Response(StoredFileSerializer(files, many=True).data)

    def post(self, request):
        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            stored = save_upload(
                request.user, serializer.validated_data["file"], serializer.validated_data["comment"]
            )
        except (OSError, DatabaseError, ValueError, RuntimeError):
            logger.error("Не удалось сохранить загруженный файл.")
            return Response(
                {"detail": "Не удалось сохранить файл."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(StoredFileSerializer(stored).data, status=status.HTTP_201_CREATED)


@method_decorator(never_cache, name="dispatch")
class FileDetailView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]
    renderer_classes = [JSONRenderer]

    def patch(self, request, file_id):
        stored = get_object_or_404(StoredFile, pk=file_id, owner=request.user)
        serializer = StoredFileUpdateSerializer(stored, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(StoredFileSerializer(stored).data)

    def delete(self, request, file_id):
        if request.data:
            return Response(
                {"detail": "DELETE не принимает тело запроса."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            delete_stored_file(request.user, file_id)
        except StoredFile.DoesNotExist:
            raise NotFound("Файл не найден.") from None
        except (DatabaseError, OSError, ValueError, RuntimeError):
            return Response(
                {"detail": "Не удалось удалить файл."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(never_cache, name="dispatch")
class FileShareView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]

    def post(self, request, file_id):
        owned_files = StoredFile.objects.filter(owner=request.user)
        try:
            origin = public_origin()
        except (ValidationError, ValueError):
            if not owned_files.filter(pk=file_id).exists():
                raise NotFound("Файл не найден.") from None
            logger.error("PUBLIC_APP_ORIGIN не настроен или имеет неверный формат.")
            return Response({"detail": "Публичный адрес приложения не настроен."}, status=503)

        try:
            token = get_share_token(owned_files, file_id)
        except StoredFile.DoesNotExist:
            raise NotFound("Файл не найден.") from None
        except DatabaseError:
            logger.error("Не удалось создать ссылку для файла %s.", file_id)
            return Response({"detail": "Не удалось создать публичную ссылку."}, status=500)
        return Response({"url": origin + reverse("shared-download", args=[token])})


def download_response(files):
    with ExitStack() as cleanup:
        try:
            stored = files.get()
            source = cleanup.enter_context(open_download(stored))
            response = FileResponse(source, as_attachment=True, filename=stored.original_name)
        except (StoredFile.DoesNotExist, OSError, ValueError, RuntimeError):
            raise NotFound("Файл недоступен.") from None

        try:
            updated = files.filter(pk=stored.pk).update(last_downloaded_at=timezone.now())
        except DatabaseError:
            logger.error("Не удалось обновить дату скачивания.")
            return Response(
                {"detail": "Не удалось подготовить скачивание."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if not updated:
            raise NotFound("Файл недоступен.")

        cleanup.pop_all()
        return response


@method_decorator(never_cache, name="dispatch")
class DownloadView(APIView):
    renderer_classes = [JSONRenderer]
    http_method_names = ["get", "options"]

    def perform_content_negotiation(self, request, force=False):
        # MIME-тип файла определяет FileResponse. JSON используется для ошибок.
        return super().perform_content_negotiation(request, force=True)


class FileDownloadView(DownloadView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, file_id):
        return download_response(StoredFile.objects.filter(owner=request.user, pk=file_id))


class SharedDownloadView(DownloadView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise NotFound("Файл недоступен.")
        return download_response(StoredFile.objects.filter(share_token=token))
