import logging
from contextlib import ExitStack

from django.db import DatabaseError
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StoredFile
from .serializers import FileUploadSerializer, StoredFileSerializer, StoredFileUpdateSerializer
from .storage import open_download, save_upload

logger = logging.getLogger(__name__)


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


@method_decorator(never_cache, name="dispatch")
class FileDownloadView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]
    http_method_names = ["get", "options"]

    def perform_content_negotiation(self, request, force=False):
        # MIME-тип файла определяет FileResponse. JSON используется для ошибок.
        return super().perform_content_negotiation(request, force=True)

    def get(self, request, file_id):
        owned_files = StoredFile.objects.filter(owner=request.user)
        stored = get_object_or_404(owned_files, pk=file_id)

        with ExitStack() as cleanup:
            try:
                source = cleanup.enter_context(open_download(stored))
                response = FileResponse(source, as_attachment=True, filename=stored.original_name)
            except (OSError, ValueError, RuntimeError):
                raise NotFound("Файл недоступен.") from None

            try:
                updated = owned_files.filter(pk=stored.pk).update(last_downloaded_at=timezone.now())
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
