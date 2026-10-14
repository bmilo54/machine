# -*- coding: utf-8 -*-

import base64
import io
import mimetypes
import string
import random
import os
import re
import nh3

from django.db import models
from django.core import validators

from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils.timezone import now
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import resolve_url
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError

try:
    from hashlib import sha1 as sha_constructor
except ImportError:
    from django.utils.hashcompat import sha_constructor


def validate_password(value):
    if len(value) < 8:
        raise ValidationError("Password must be at least 8 characters long.")
    if not re.search(r"[a-z]", value):
        raise ValidationError("Password must contain at least one lowercase letter.")
    if not re.search(r"[A-Z]", value):
        raise ValidationError("Password must contain at least one uppercase letter.")
    if not re.search(r"[0-9]", value):
        raise ValidationError("Password must contain at least one number.")
    if not re.search(r"[^a-zA-Z0-9]", value):  # Includes all non-alphanumeric characters
        raise ValidationError("Password must contain at least one special character.")


def generate_sha1(string, salt=None):
    if not salt:
        salt = sha_constructor(str(random.random()).encode('utf-8')).hexdigest()[:5]
    hash_key = sha_constructor("{0}{1}".format(str(salt), str(string)).encode('utf-8')).hexdigest()

    return (salt, hash_key)


def safe_referrer(request, default):
    """
    Takes the request and a default URL. Returns HTTP_REFERER if it's safe
    to use and set, and the default URL otherwise.

    The default URL can be a model with get_absolute_url defined, a urlname
    or a regular URL
    """
    referrer = request.META.get('HTTP_REFERER')
    if referrer and url_has_allowed_host_and_scheme(referrer, request.get_host()):
        return referrer
    if default:
        # Try to resolve. Can take a model instance, Django URL name or URL.
        return resolve_url(default)
    else:
        # Allow passing in '' and None as default
        return default


def random_string_generator(size, additional=None, chars=string.ascii_uppercase + string.digits + string.ascii_lowercase):
    return ''.join(random.choice(chars + str(additional)) for _ in range(size))


def random_string_generator_v2(size, additional=None):
    # Build allowed characters
    exclude = {'O', 'o', 'l', 'I'}
    chars = ''.join(c for c in (string.ascii_uppercase + string.ascii_lowercase + string.digits) if c not in exclude)

    # Add any extra characters
    if additional:
        chars += str(additional)

    return ''.join(random.choice(chars) for _ in range(size))


def get_protocol():
    """
    Returns a string with the current protocol.

    This can be either 'http' or 'https' depending on setting.
    """
    protocol = 'http'
    if settings.USE_HTTPS:
        protocol = 'https'
    return protocol


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def running_number_generator(latest_code, prefix_length=None):
    running_number = latest_code
    number = running_number

    new_number = int(number) + 1
    if new_number < 100000:
        alphabet = running_number[:0]
    else:
        ascii_code = ord(running_number[:1])  # get alphabet int value
        alphabet = chr(ascii_code + 1)  # convert int value to alphabet
        new_number = 0
    zeros = len(str(new_number))

    for x in range(6 - zeros):  # loop for numbers of zeros
        alphabet += "0"

    new_code = alphabet + str(int(number) + 1)  # new running number

    return new_code


def running_number_generator_v2(latest_code):
    """
    Generate next running number with alphabetic prefix rollover.
    Examples: 
        001 → 002
        999 → A001 (rollover: add prefix A)
        A001 → A002
        A999 → B001 (rollover: increment prefix to B)
    """
    # Find where the number starts
    prefix = ''
    for i, char in enumerate(latest_code):
        if char.isdigit():
            prefix = latest_code[:i]
            number = int(latest_code[i:])
            break
    
    # Increment number
    new_number = number + 1
    
    # Check for rollover (number would become 1000)
    if new_number > 999:
        if prefix == '':
            # No prefix yet, add 'A'
            prefix = 'A'
        else:
            # Increment last character of prefix
            prefix = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        new_number = 1  # Reset to 001
    
    # Format with leading zeros (always 3 digits)
    return prefix + str(new_number).zfill(3)

class JSONResponseMixin(object):
    """
    A mixin that can be used to render a JSON response.
    """

    def render_to_json_response(self, context, **response_kwargs):
        """
        Returns a JSON response, transforming 'context' to make the payload.
        """
        return JsonResponse(
            self.get_data(context),
            **response_kwargs
        )

    def get_data(self, context):
        """
        Returns an object that will be serialized as JSON by json.dumps().
        """
        # Note: This is *EXTREMELY* naive; in reality, you'll need
        # to do much more complex handling to ensure that arbitrary
        # objects -- such as Django model instances or querysets
        # -- can be serialized as JSON.
        return context


def logout_user_everywhere(user):
    """
    Deletes all active sessions for the given user.
    """
    # Get only active (non-expired) sessions
    active_sessions = Session.objects.filter(expire_date__gte=now())

    # Bulk scan
    for session in active_sessions:
        data = session.get_decoded()
        if str(user.id) == str(data.get('_auth_user_id')):
            session.delete()