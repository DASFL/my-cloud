import logging
import re
from contextlib import ExitStack

from django.db import DatabaseError, transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAdminUser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from files.storage import open_storage_directory, remove_storage_files, storage_file_names

from .models import User
from .serializers import AdminRoleSerializer, AdminUserSerializer

logger = logging.getLogger(__name__)


def validate_user_id(user_id):
    if not re.fullmatch(r"[0-9]{1,19}", user_id) or int(user_id) > 9223372036854775807:
        raise NotFound("Пользователь не найден.")


def locked_administrators(actor):
    admins = list(User.objects.select_for_update().filter(is_staff=True).order_by("pk"))
    if not any(user.pk == actor.pk and user.is_active for user in admins):
        raise PermissionDenied("Требуются права администратора.")
    return admins


def protect_last_admin(user, admins):
    if user.is_staff and user.is_active and user.has_usable_password():
        remaining = any(
            admin.pk != user.pk and admin.is_active and admin.has_usable_password()
            for admin in admins
        )
        if not remaining:
            raise ValidationError({"is_staff": ["Нельзя лишить систему последнего действующего администратора."]})


def users_with_storage_totals():
    return User.objects.annotate(
        files_count=Count("stored_files"),
        total_size_bytes=Sum("stored_files__size_bytes", default=0),
    ).order_by("username")


@method_decorator(never_cache, name="dispatch")
class AdminUserListView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]
    renderer_classes = [JSONRenderer]

    def get(self, request):
        return Response(AdminUserSerializer(users_with_storage_totals(), many=True).data)


@method_decorator(never_cache, name="dispatch")
class AdminUserDetailView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]
    parser_classes = [JSONParser]
    renderer_classes = [JSONRenderer]

    def patch(self, request, user_id):
        validate_user_id(user_id)
        serializer = AdminRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            admins = locked_administrators(request.user)
            user = get_object_or_404(User, pk=int(user_id))
            if not serializer.validated_data["is_staff"] and user.is_staff:
                protect_last_admin(user, admins)
            user.is_staff = serializer.validated_data["is_staff"]
            user.save(update_fields=["is_staff"])
            result = users_with_storage_totals().get(pk=user.pk)
        return Response(AdminUserSerializer(result).data)

    def delete(self, request, user_id):
        validate_user_id(user_id)
        if request.body:
            raise ValidationError({"detail": "DELETE не принимает тело запроса."})
        committed = False
        try:
            with ExitStack() as directories:
                with transaction.atomic(durable=True):
                    admins = locked_administrators(request.user)
                    user = get_object_or_404(User.objects.select_for_update(), pk=int(user_id))
                    protect_last_admin(user, admins)
                    if user.pk == request.user.pk:
                        raise ValidationError({"detail": "Нельзя удалить самого себя."})
                    files = list(user.stored_files.select_for_update())
                    for stored in files:
                        stored.storage_path
                    owner_fd = directories.enter_context(open_storage_directory(user.storage_relpath))
                    names = storage_file_names(owner_fd)
                    user.delete()
                committed = True
                remove_storage_files(owner_fd, names)
        except (DatabaseError, OSError, ValueError, RuntimeError) as error:
            if committed:
                logger.error(
                    "Пользователь %s удалён из БД, очистка файлов не завершена (%s, errno=%s). "
                    "Проверьте cleanup_orphan_files.", user_id, type(error).__name__,
                    getattr(error, "errno", None),
                )
            else:
                logger.error(
                    "Удаление пользователя %s не подтверждено, файлы сохранены (%s, errno=%s).",
                    user_id, type(error).__name__, getattr(error, "errno", None),
                )
            return Response({"detail": "Не удалось завершить удаление пользователя."}, status=500)
        return Response(status=204)
