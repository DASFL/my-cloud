from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from psycopg.errors import UniqueViolation
from rest_framework import serializers

from .models import User


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(trim_whitespace=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class SessionUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "full_name", "email", "is_staff")
        read_only_fields = fields


class AdminUserSerializer(SessionUserSerializer):
    files_count = serializers.IntegerField(read_only=True)
    total_size_bytes = serializers.IntegerField(read_only=True)

    class Meta(SessionUserSerializer.Meta):
        fields = SessionUserSerializer.Meta.fields + ("files_count", "total_size_bytes")
        read_only_fields = fields


class AdminRoleSerializer(serializers.Serializer):
    is_staff = serializers.BooleanField()

    def validate_is_staff(self, value):
        if not isinstance(self.initial_data["is_staff"], bool):
            raise serializers.ValidationError("Передайте true или false.")
        return value

    def validate(self, attrs):
        unexpected = {
            name: ["Это поле нельзя изменять."]
            for name in self.initial_data if name != "is_staff"
        }
        if unexpected:
            raise serializers.ValidationError(unexpected)
        return attrs


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ("id", "username", "full_name", "email", "password")
        read_only_fields = ("id",)
        extra_kwargs = {"username": {"trim_whitespace": False}}

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from None
        return value

    def validate(self, attrs):
        unexpected = {
            name: ["Это поле нельзя передавать при регистрации."]
            for name in self.initial_data
            if name not in self.fields or self.fields[name].read_only
        }
        if unexpected:
            raise serializers.ValidationError(unexpected)
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return User.objects.create_user(**validated_data)
        except IntegrityError as error:
            cause = error.__cause__
            if (
                isinstance(cause, UniqueViolation)
                and cause.diag.constraint_name == "accounts_user_username_key"
            ):
                raise serializers.ValidationError({"username": ["Этот логин уже занят."]}) from None
            raise
