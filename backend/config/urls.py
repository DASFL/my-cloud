from django.urls import path

from accounts.views import CsrfCookieView, CurrentUserView, LoginView, LogoutView, RegistrationView
from files.views import FileListView

from .views import health

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/auth/csrf/", CsrfCookieView.as_view(), name="csrf"),
    path("api/auth/register/", RegistrationView.as_view(), name="register"),
    path("api/auth/login/", LoginView.as_view(), name="login"),
    path("api/auth/me/", CurrentUserView.as_view(), name="me"),
    path("api/auth/logout/", LogoutView.as_view(), name="logout"),
    path("api/files/", FileListView.as_view(), name="files"),
]
