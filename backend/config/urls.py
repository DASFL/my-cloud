from django.urls import path

from accounts.views import RegistrationView

from .views import health

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/auth/register/", RegistrationView.as_view(), name="register"),
]
