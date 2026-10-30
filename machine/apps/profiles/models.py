from django.contrib.auth.models import User
from django.db import models

from core.abstract_models import TimeStampedModel


class Profile(TimeStampedModel):
    """
    Django 用户与阿里云无影终端用户的对应关系。
    分配桌面时, API 使用 eds_end_user_id, 而不是 Django 用户名。
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name="User", related_name="profile")
    eds_end_user_id = models.CharField(
        verbose_name="EDS End User ID",
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        help_text="Alibaba EDS end user ID used when assigning a desktop",
    )
    wuying_password = models.CharField(
        verbose_name="Wuying password",
        max_length=15,
        blank=True,
        help_text="Last password the dashboard set in Alibaba, so an admin can send it to the user.",
    )

    class Meta:
        verbose_name = "Profile"
        verbose_name_plural = "Profiles"

    def __str__(self):
        return f"{self.user} ({self.eds_end_user_id or '-'})"
