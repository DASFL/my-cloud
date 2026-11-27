from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAdminUser

from accounts.admin_views import validate_user_id
from accounts.models import User

from .views import FileDetailView, FileDownloadView, FileListView, FileShareView


class AdminFileOwnerMixin:
    permission_classes = [IsAdminUser]

    def get_owner(self, request):
        user_id = self.kwargs["user_id"]
        validate_user_id(user_id)
        return get_object_or_404(User, pk=int(user_id))


class AdminFileListView(AdminFileOwnerMixin, FileListView):
    pass


class AdminFileDetailView(AdminFileOwnerMixin, FileDetailView):
    pass


class AdminFileDownloadView(AdminFileOwnerMixin, FileDownloadView):
    pass


class AdminFileShareView(AdminFileOwnerMixin, FileShareView):
    pass
