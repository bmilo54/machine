from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("virtuals", "0011_activity_region"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScreenSummary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, verbose_name="Created Date")),
                ("modified", models.DateTimeField(auto_now=True, verbose_name="Modified Date")),
                ("file_path", models.CharField(max_length=1000, unique=True)),
                ("desktop_id", models.CharField(blank=True, max_length=200)),
                ("desktop_name", models.CharField(blank=True, max_length=255)),
                ("end_user_id", models.CharField(blank=True, max_length=200)),
                ("started", models.DateTimeField(blank=True, null=True)),
                ("finished", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(default="done", max_length=20)),
                ("detail", models.TextField(blank=True)),
                ("lines", models.JSONField(blank=True, default=list)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.user",
                        verbose_name="Created By",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.user",
                        verbose_name="Modified By",
                    ),
                ),
            ],
            options={
                "ordering": ["-started", "-created"],
            },
        ),
    ]
