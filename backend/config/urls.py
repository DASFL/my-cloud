from django.urls import path

from accounts.admin_views import AdminUserDetailView, AdminUserListView
from accounts.views import CsrfCookieView, CurrentUserView, LoginView, LogoutView, RegistrationView
from files.admin_views import AdminFileDetailView, AdminFileDownloadView, AdminFileListView, AdminFileShareView
from files.views import FileDetailView, FileDownloadView, FileListView, FileShareView, SharedDownloadView

from .views import health

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/auth/csrf/", CsrfCookieView.as_view(), name="csrf"),
    path("api/auth/register/", RegistrationView.as_view(), name="register"),
    path("api/auth/login/", LoginView.as_view(), name="login"),
    path("api/auth/me/", CurrentUserView.as_view(), name="me"),
    path("api/auth/logout/", LogoutView.as_view(), name="logout"),
    path("api/admin/users/", AdminUserListView.as_view(), name="admin-users"),
    path("api/admin/users/<str:user_id>/", AdminUserDetailView.as_view(), name="admin-user-detail"),
    path("api/admin/users/<str:user_id>/files/", AdminFileListView.as_view(), name="admin-files"),
    path("api/admin/users/<str:user_id>/files/<uuid:file_id>/", AdminFileDetailView.as_view(), name="admin-file-detail"),
    path("api/admin/users/<str:user_id>/files/<uuid:file_id>/download/", AdminFileDownloadView.as_view(), name="admin-file-download"),
    path("api/admin/users/<str:user_id>/files/<uuid:file_id>/share/", AdminFileShareView.as_view(), name="admin-file-share"),
    path("api/files/", FileListView.as_view(), name="files"),
    path("api/files/<uuid:file_id>/", FileDetailView.as_view(), name="file-detail"),
    path("api/files/<uuid:file_id>/download/", FileDownloadView.as_view(), name="file-download"),
    path("api/files/<uuid:file_id>/share/", FileShareView.as_view(), name="file-share"),
    path("api/shared/<str:token>/download/", SharedDownloadView.as_view(), name="shared-download"),
]
