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
