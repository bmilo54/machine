from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("virtuals", "0012_screen_summary"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, verbose_name="Created Date")),
                ("modified", models.DateTimeField(auto_now=True, verbose_name="Modified Date")),
                ("end_user_id", models.CharField(max_length=200)),
                ("day", models.DateField()),
                ("report", models.TextField(blank=True)),
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
                "ordering": ["-day", "end_user_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="workreport",
            constraint=models.UniqueConstraint(fields=("end_user_id", "day"), name="unique_work_report_day"),
        ),
    ]
