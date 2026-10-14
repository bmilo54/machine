from django.db.models import DateTimeField
import time
import logging
import uuid

from django import forms
from django.core.cache import cache
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.encoding import smart_str
from django.utils.translation import gettext_lazy as _
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

class GroupRequiredMixin(object):
    group_required = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise PermissionDenied
        else:
            user_groups = []

            for group in request.user.groups.values_list('name', flat=True):
                user_groups.append(group)

            if len(set(user_groups).intersection(self.group_required)) <= 0:
                raise PermissionDenied

        return super(GroupRequiredMixin, self).dispatch(request, *args, **kwargs)

class DashboardMixin(LoginRequiredMixin):
    login_url = reverse_lazy('view_dashboards:login')
    redirect_field_name = 'redirect_to'

    is_superadmin = False
    is_staff = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super(DashboardMixin, self).dispatch(request, *args, **kwargs)
        self.is_superadmin = request.user.groups.filter(name="superadmin").exists()
        self.is_staff = request.user.groups.filter(name="staff").exists()
        if not request.user.groups.all():
            logout(request)
            messages.error(request, "You don't have permission to access this page.")
            return redirect(reverse("view_dashboards:login"))

        return super(DashboardMixin, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(DashboardMixin, self).get_context_data(**kwargs)
        context['is_superadmin'] = self.is_superadmin
        context['is_staff'] = self.is_staff

        return context