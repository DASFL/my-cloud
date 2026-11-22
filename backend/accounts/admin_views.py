import re

from django.db import transaction
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

from .models import User
from .serializers import AdminRoleSerializer, AdminUserSerializer


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
        if not re.fullmatch(r"[0-9]{1,19}", user_id) or int(user_id) > 9223372036854775807:
            raise NotFound("Пользователь не найден.")
        serializer = AdminRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # Общая блокировка администраторов не даёт одновременно снять обе роли.
            admins = list(User.objects.select_for_update().filter(is_staff=True).order_by("pk"))
            if not any(user.pk == request.user.pk and user.is_active for user in admins):
                raise PermissionDenied("Требуются права администратора.")
            user = get_object_or_404(User, pk=int(user_id))
            if not serializer.validated_data["is_staff"] and user.is_staff:
                remaining = any(
                    admin.pk != user.pk and admin.is_active and admin.has_usable_password()
                    for admin in admins
                )
                if user.is_active and user.has_usable_password() and not remaining:
                    raise ValidationError({"is_staff": ["Нельзя снять права последнего действующего администратора."]})
            user.is_staff = serializer.validated_data["is_staff"]
            user.save(update_fields=["is_staff"])
            result = users_with_storage_totals().get(pk=user.pk)
        return Response(AdminUserSerializer(result).data)
