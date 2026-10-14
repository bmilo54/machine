from django.utils import timezone
from django.db import models
from django.utils.timezone import localtime
from django.utils.html import format_html
from django.contrib.auth.models import User
from django.utils.safestring import mark_safe

class TimeStampedModel(models.Model):
    created_by = models.ForeignKey(
        User,
        verbose_name="Created By",
        related_name="+",
        on_delete=models.SET_NULL,
        blank=True, null=True
    )

    modified_by = models.ForeignKey(
        User,
        verbose_name="Modified By",
        related_name="+",
        on_delete=models.SET_NULL,
        blank=True, null=True
    )

    created = models.DateTimeField(
        verbose_name="Created Date",
        auto_now_add=True
    )

    modified = models.DateTimeField(
        verbose_name="Modified Date",
        auto_now=True
    )

    class Meta:
        abstract = True
        get_latest_by = 'created'

    def admin_created(self):
        """
        Return admin display created.
        """
        if self.created_by:
            return format_html(
                mark_safe("{}<br /> {}".format(
                    self.created_by, localtime(self.created).strftime("%Y-%m-%d<br /> %I:%M %p")
                ))
            )
        else:
            return format_html(mark_safe("{}".format(localtime(self.created).strftime("%Y-%m-%d<br /> %I:%M %p"))))

    admin_created.admin_order_field = 'created'
    admin_created.short_description = 'Created'

    def admin_modified(self):
        """
        Return admin display modified.
        """
        if self.modified_by:
            return format_html(
                mark_safe("{}<br /> {}".format(
                    self.modified_by, localtime(self.modified).strftime("%Y-%m-%d<br /> %I:%M %p")
                ))
            )
        else:
            return format_html(mark_safe("{}".format(localtime(self.modified).strftime("%Y-%m-%d<br /> %I:%M %p"))))

    admin_modified.admin_order_field = 'modified'
    admin_modified.short_description = 'Modified'

class AbstractOrderableModel(models.Model):
    """An abstract model that provides orderable function based on `sort_order` fields."""

    sort_order = models.PositiveSmallIntegerField(verbose_name="Ordering", default=0, db_index=True)

    class Meta:
        abstract = True
        ordering = ['sort_order']

    def save(self, *args, **kwargs):
        model = self.__class__

        # Auto calculate sort_order
        if self.sort_order == 0:
            try:
                last = model.objects.order_by('-sort_order')[0]
                self.sort_order = last.sort_order + 1
            except IndexError:
                # This item is first row
                self.sort_order = 1

        super().save(*args, **kwargs)