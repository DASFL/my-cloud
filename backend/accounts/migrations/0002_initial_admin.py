from django.contrib.auth.hashers import make_password
from django.db import migrations


def create_initial_admin(apps, schema_editor):
    user_model = apps.get_model("accounts", "User")
    user_model.objects.using(schema_editor.connection.alias).get_or_create(
        username="admin",
        defaults={
            "full_name": "Администратор",
            "email": "admin@example.invalid",
            "password": make_password(None),
            "is_staff": True,
            "is_active": False,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.RunPython(create_initial_admin, migrations.RunPython.noop),
    ]
