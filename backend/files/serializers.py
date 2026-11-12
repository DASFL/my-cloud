from unicodedata import category

from django.conf import settings
from rest_framework import serializers

from .models import StoredFile


class FileUploadSerializer(serializers.Serializer):
    file = serializers.FileField(allow_empty_file=True, max_length=255)
    comment = serializers.CharField(required=False, allow_blank=True, default="", trim_whitespace=False)

    def validate_file(self, uploaded_file):
        if uploaded_file.size > settings.FILE_UPLOAD_MAX_BYTES:
            raise serializers.ValidationError(
                f"Размер файла превышает {settings.FILE_UPLOAD_MAX_BYTES} байт."
            )
        return uploaded_file

    def validate(self, attrs):
        unexpected = {
            name: ["Это поле нельзя передавать при загрузке."]
            for name in self.initial_data.keys() - self.fields.keys()
        }
        if unexpected:
            raise serializers.ValidationError(unexpected)
        if len(self.initial_data.getlist("file")) > 1:
            raise serializers.ValidationError({"file": ["Загрузите один файл за запрос."]})
        return attrs


class StoredFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoredFile
        fields = ("id", "original_name", "comment", "size_bytes", "created_at", "last_downloaded_at")
        read_only_fields = fields


class StoredFileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoredFile
        fields = ("original_name", "comment")
        extra_kwargs = {
            "original_name": {"trim_whitespace": False},
            "comment": {"trim_whitespace": False},
        }

    def to_internal_value(self, data):
        if isinstance(data, dict):
            errors = {}
            for name, value in data.items():
                if name not in self.fields:
                    errors[name] = ["Это поле нельзя изменять."]
                elif not isinstance(value, str):
                    errors[name] = ["Значение должно быть строкой."]
            if errors:
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)

    def validate_original_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Укажите имя файла.")
        if name in {".", ".."}:
            raise serializers.ValidationError("Имя файла не может быть «.» или «..».")
        if any(char in "/\\" or category(char) in {"Cc", "Cf"} for char in value):
            raise serializers.ValidationError(
                "Имя файла не должно содержать разделители путей или управляющие символы."
            )
        return value

    def update(self, instance, validated_data):
        if validated_data:
            for name, value in validated_data.items():
                setattr(instance, name, value)
            instance.save(update_fields=validated_data)
        return instance
