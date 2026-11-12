import logging

from django.db import DatabaseError
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StoredFile
from .serializers import FileUploadSerializer, StoredFileSerializer
from .storage import save_upload

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
