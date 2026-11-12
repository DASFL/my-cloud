from django.urls import path

from accounts.views import CsrfCookieView, CurrentUserView, LoginView, LogoutView, RegistrationView
from files.views import FileDetailView, FileDownloadView, FileListView, FileShareView, SharedDownloadView

from .views import health

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/auth/csrf/", CsrfCookieView.as_view(), name="csrf"),
    path("api/auth/register/", RegistrationView.as_view(), name="register"),
    path("api/auth/login/", LoginView.as_view(), name="login"),
    path("api/auth/me/", CurrentUserView.as_view(), name="me"),
    path("api/auth/logout/", LogoutView.as_view(), name="logout"),
    path("api/files/", FileListView.as_view(), name="files"),
    path("api/files/<uuid:file_id>/", FileDetailView.as_view(), name="file-detail"),
    path("api/files/<uuid:file_id>/download/", FileDownloadView.as_view(), name="file-download"),
    path("api/files/<uuid:file_id>/share/", FileShareView.as_view(), name="file-share"),
    path("api/shared/<str:token>/download/", SharedDownloadView.as_view(), name="shared-download"),
]
