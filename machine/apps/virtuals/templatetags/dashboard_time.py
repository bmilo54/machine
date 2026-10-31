from django import template
from django.utils.timezone import localtime

register = template.Library()


@register.filter
def dash_datetime(value):
    if not value:
        return ""
    text = localtime(value).strftime("%d/%m/%y %I:%M %p")
    return text[:-2] + text[-2:].lower()
