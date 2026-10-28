from django.contrib.auth.models import User
from django.db import models

from core.abstract_models import TimeStampedModel


class Profile(TimeStampedModel):
    """
    Links a dashboard user to a WorkSpaces directory username.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name="User", related_name="profile")
    eds_end_user_id = models.CharField(
        verbose_name="Directory username",
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        help_text="WorkSpaces directory username used when assigning a desktop",
    )

    class Meta:
        verbose_name = "Profile"
        verbose_name_plural = "Profiles"

    def __str__(self):
        return f"{self.user} ({self.eds_end_user_id or '-'})"
