import logging

from django.shortcuts import get_object_or_404
from django.views.generic.edit import CreateView, FormView, UpdateView, DeleteView
from django.views.generic.base import RedirectView, TemplateView
from django.views.generic import DetailView, ListView, View

from django.utils import timezone
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.http import HttpResponseRedirect, HttpResponse, JsonResponse
from django.contrib import messages
from django.db.models import Count, Q
from django.conf import settings
from django.contrib.auth import login, logout, authenticate

from apps.profiles.models import Profile
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine
from core.mixins import DashboardMixin, GroupRequiredMixin
from view_api.views import (
    AlibabaApiError,
    REGION_ID,
    apply_desktop,
    connection_ticket,
    create_cloud_desktop,
    delete_cloud_desktop,
    describe_desktops,
    operate_desktop,
    write_log,
)
from .forms import DashboardLoginForm, DesktopCreateForm, UserProfileForm, VMProfileForm, VirtualMachineForm

class LoginView(FormView):
    form_class = DashboardLoginForm
    success_url = reverse_lazy('view_dashboards:dashboard')
    template_name = 'view_dashboards/login.html'

    is_superadmin = False
    is_staff = False

    @method_decorator(sensitive_post_parameters())
    @method_decorator(csrf_protect)
    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        if self.request.user.is_authenticated:
            redirect_to = self.get_success_url()
            if redirect_to == self.request.path:
                raise ValueError(
                    "Redirection loop for authenticated user detected. Check that "
                    "your LOGIN_REDIRECT_URL does't point to a login page."
                )
            return HttpResponseRedirect(redirect_to)
        return super(LoginView, self).dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        username = form.cleaned_data['username']
        password = form.cleaned_data['password']
        user = authenticate(username=username, password=password)

        if user is not None:
            self.is_superadmin = user.groups.filter(name='superadmin').exists()
            self.is_staff = user.groups.filter(name='staff').exists()

            if self.is_superadmin or self.is_staff:
                pass
            else:
                return self.form_invalid(form)

            if user.is_active:
                login(self.request, user)

                return super(LoginView, self).form_valid(form)
            else:
                return self.form_invalid(form)
        else:
            return self.form_invalid(form)

    def form_invalid(self, form):
        error_len = 0
        error_len += len(form.errors)

        messages.error(self.request, "Please enter a correct username and password")
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super(LoginView, self).get_context_data(**kwargs)
        context['page_title'] = "Login"
        context['selected_page'] = "login"

        return context

    def get_success_url(self):
        messages.success(self.request, "Welcome Back {}.".format(self.request.user))
        if self.is_superadmin:
            return self.success_url

        return super(LoginView, self).get_success_url()

class LogoutView(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        if self.is_superadmin or self.is_staff:
            self.url = reverse_lazy('view_dashboards:login')
        else:
            self.url = reverse_lazy('view_dashboards:login')

        return super(LogoutView, self).get_redirect_url(*args, **kwargs)

    def get(self, request, *args, **kwargs):
        self.is_superadmin = request.user.groups.filter(name='superadmin').exists()
        self.is_staff = request.user.groups.filter(name='staff').exists()
        logout(request)

        return super(LogoutView, self).get(request, *args, **kwargs)

class DashboardPageView(DashboardMixin, TemplateView):
    def get_context_data(self, **kwargs):
        context = super(DashboardPageView, self).get_context_data(**kwargs)
        context['page_title'] = "Dashboard"
        context['selected_page'] = "dashboard"
        context['main_nav'] = "dashboard"
        context['hide_breadcrumb'] = True
        context['stats'] = VirtualMachine.objects.aggregate(
            total=Count("id"),
            running=Count("id", filter=Q(status="Running")),
            stopped=Count("id", filter=Q(status="Stopped")),
            creating=Count("id", filter=Q(status="Creating")),
            error=Count("id", filter=Q(status="Error")),
        )
        context['recent_logs'] = ActivityLog.objects.select_related("actor", "virtual_machine").order_by("-created")[:8]
        return context

    def get_template_names(self):
        # Only the superadmin dashboard exists for now; reuse it for any
        # authenticated dashboard user until dedicated staff templates exist.
        self.template_name = "view_dashboards/_group_dashboard/superadmin/dashboard.html"
        return super(DashboardPageView, self).get_template_names()


class DesktopListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_list.html"
    context_object_name = "desktops"
    paginate_by = 20

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user", "current_profile").order_by("-created")

    def get_context_data(self, **kwargs):
        context = super(DesktopListView, self).get_context_data(**kwargs)
        context["page_title"] = "Desktops"
        context["selected_page"] = "desktops"
        context["main_nav"] = "desktops"
        return context


class DesktopDetailView(GroupRequiredMixin, DashboardMixin, DetailView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_detail.html"
    context_object_name = "desktop"

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user", "current_profile")

    def get_context_data(self, **kwargs):
        context = super(DesktopDetailView, self).get_context_data(**kwargs)
        context["page_title"] = self.object.name or "Desktop"
        context["selected_page"] = "desktops"
        context["main_nav"] = "desktops"
        context["activity_logs"] = self.object.activity_logs.select_related("actor").order_by("-created")[:30]
        return context


def client_ip(request):
    return request.META.get('REMOTE_ADDR') or None


def apply_profile_defaults(vm):
    profile = vm.current_profile
    if not profile:
        return
    if vm.cpu is None:
        vm.cpu = profile.cpu
    if vm.ram is None:
        vm.ram = profile.ram
    if vm.system_disk in (None, 0) and profile.system_disk:
        vm.system_disk = profile.system_disk
    if vm.data_disk in (None, 0) and profile.data_disk:
        vm.data_disk = profile.data_disk


def log_desktop(request, vm, action, old_value='', new_value='', details=''):
    ActivityLog.objects.create(
        actor=request.user,
        virtual_machine=vm,
        action=action,
        old_value=old_value,
        new_value=new_value,
        details=details,
        ip_address=client_ip(request),
        created_by=request.user,
    )


class DashboardFormMixin(DashboardMixin):
    template_name = 'view_dashboards/_group_dashboard/superadmin/form.html'
    page_subtitle = ''
    cancel_url_name = ''

    def get_context_data(self, **kwargs):
        context = super(DashboardFormMixin, self).get_context_data(**kwargs)
        context['page_subtitle'] = self.page_subtitle
        context['cancel_url'] = reverse(self.cancel_url_name)

        return context


class DesktopCreateView(GroupRequiredMixin, DashboardFormMixin, CreateView):
    group_required = ['superadmin']
    model = VirtualMachine
    form_class = DesktopCreateForm
    page_subtitle = 'Creates a Windows desktop in Amazon WorkSpaces. Billing is hourly while the desktop is running.'
    cancel_url_name = 'view_dashboards:desktop_list'

    def get_context_data(self, **kwargs):
        context = super(DesktopCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Create cloud computer'
        context['main_nav'] = 'desktops'
        context['submit_label'] = 'Create cloud computer'
        return context

    def form_valid(self, form):
        vm = form.save(commit=False)
        try:
            created = create_cloud_desktop(
                vm.name,
                vm.current_profile,
                form.cleaned_data["directory_username"],
            )
        except AlibabaApiError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        vm.vm_id = created["vm_id"]
        vm.cpu = created["cpu"]
        vm.ram = created["ram"]
        vm.system_disk = created["system_disk"]
        vm.data_disk = created["data_disk"]
        vm.status = "Creating"
        vm.region = REGION_ID
        vm.directory_id = settings.DIRECTORY_ID if settings.DIRECTORY_ID != "xxxx" else ""
        vm.created_by = self.request.user
        vm.save()
        self.object = vm
        log_desktop(
            self.request,
            vm,
            'Assign' if vm.user_id else None,
            new_value=str(vm.user or ''),
            details='Created an Amazon WorkSpace, AutoStop hourly billing',
        )
        messages.success(
            self.request,
            'AWS is creating this desktop. Billing is hourly while it is running. Sync from AWS to refresh the status.',
        )
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:desktop_detail', args=[self.object.pk])


class DesktopUpdateView(GroupRequiredMixin, DashboardFormMixin, UpdateView):
    group_required = ['superadmin']
    model = VirtualMachine
    form_class = VirtualMachineForm
    page_subtitle = 'Update the desktop assignment and hardware record.'
    cancel_url_name = 'view_dashboards:desktop_list'

    def get_context_data(self, **kwargs):
        context = super(DesktopUpdateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Edit desktop'
        context['main_nav'] = 'desktops'
        context['cancel_url'] = reverse('view_dashboards:desktop_detail', args=[self.object.pk])
        return context

    def form_valid(self, form):
        previous = VirtualMachine.objects.select_related('user', 'current_profile').get(pk=self.object.pk)
        vm = form.save(commit=False)
        vm.modified_by = self.request.user
        vm.save()
        self.object = vm

        if previous.user_id != vm.user_id:
            action = 'Assign' if vm.user_id else 'Unassign'
            old_value = str(previous.user or '')
            new_value = str(vm.user or '')
            details = 'Changed assigned user'
        elif (
            previous.cpu != vm.cpu
            or previous.ram != vm.ram
            or previous.system_disk != vm.system_disk
            or previous.data_disk != vm.data_disk
            or previous.current_profile_id != vm.current_profile_id
        ):
            action = 'Resize'
            old_value = '{}C / {}GB'.format(previous.cpu, previous.ram)
            new_value = '{}C / {}GB'.format(vm.cpu, vm.ram)
            details = 'Changed hardware'
        else:
            action = None
            old_value = ''
            new_value = ''
            details = 'Updated desktop'

        log_desktop(self.request, vm, action, old_value, new_value, details)
        messages.success(self.request, 'Desktop updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:desktop_detail', args=[self.object.pk])


class VMProfileListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = VMProfile
    template_name = 'view_dashboards/_group_dashboard/superadmin/vm_profile_list.html'
    context_object_name = 'vm_profiles'
    paginate_by = 20

    def get_queryset(self):
        return VMProfile.objects.order_by('name')

    def get_context_data(self, **kwargs):
        context = super(VMProfileListView, self).get_context_data(**kwargs)
        context['page_title'] = 'VM Profiles'
        context['main_nav'] = 'vm_profiles'
        return context


class VMProfileCreateView(GroupRequiredMixin, DashboardFormMixin, CreateView):
    group_required = ['superadmin']
    model = VMProfile
    form_class = VMProfileForm
    page_subtitle = 'Hardware template used when a desktop is created or resized.'
    cancel_url_name = 'view_dashboards:vm_profile_list'

    def get_context_data(self, **kwargs):
        context = super(VMProfileCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Add VM profile'
        context['main_nav'] = 'vm_profiles'
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.created_by = self.request.user
        profile.save()
        self.object = profile
        messages.success(self.request, 'VM profile saved.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:vm_profile_list')


class VMProfileUpdateView(GroupRequiredMixin, DashboardFormMixin, UpdateView):
    group_required = ['superadmin']
    model = VMProfile
    form_class = VMProfileForm
    page_subtitle = 'Update this hardware template.'
    cancel_url_name = 'view_dashboards:vm_profile_list'

    def get_context_data(self, **kwargs):
        context = super(VMProfileUpdateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Edit VM profile'
        context['main_nav'] = 'vm_profiles'
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.modified_by = self.request.user
        profile.save()
        self.object = profile
        messages.success(self.request, 'VM profile updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:vm_profile_list')


class UserProfileListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = Profile
    template_name = 'view_dashboards/_group_dashboard/superadmin/user_profile_list.html'
    context_object_name = 'user_profiles'
    paginate_by = 20

    def get_queryset(self):
        return Profile.objects.select_related('user').order_by('user__username')

    def get_context_data(self, **kwargs):
        context = super(UserProfileListView, self).get_context_data(**kwargs)
        context['page_title'] = 'Users'
        context['main_nav'] = 'users'
        return context


class UserProfileCreateView(GroupRequiredMixin, DashboardFormMixin, CreateView):
    group_required = ['superadmin']
    model = Profile
    form_class = UserProfileForm
    page_subtitle = 'Link a dashboard user to a WorkSpaces directory username.'
    cancel_url_name = 'view_dashboards:user_profile_list'

    def get_context_data(self, **kwargs):
        context = super(UserProfileCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Add user profile'
        context['main_nav'] = 'users'
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.created_by = self.request.user
        profile.save()
        self.object = profile
        messages.success(self.request, 'User profile saved.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:user_profile_list')


class UserProfileUpdateView(GroupRequiredMixin, DashboardFormMixin, UpdateView):
    group_required = ['superadmin']
    model = Profile
    form_class = UserProfileForm
    page_subtitle = 'Update the WorkSpaces directory username for this account.'
    cancel_url_name = 'view_dashboards:user_profile_list'

    def get_context_data(self, **kwargs):
        context = super(UserProfileUpdateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Edit user profile'
        context['main_nav'] = 'users'
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.modified_by = self.request.user
        profile.save()
        self.object = profile
        messages.success(self.request, 'User profile updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:user_profile_list')


class ActivityLogListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = ActivityLog
    template_name = "view_dashboards/_group_dashboard/superadmin/activity_list.html"
    context_object_name = "activity_logs"
    paginate_by = 30

    def get_queryset(self):
        return ActivityLog.objects.select_related("actor", "virtual_machine").order_by("-created")

    def get_context_data(self, **kwargs):
        context = super(ActivityLogListView, self).get_context_data(**kwargs)
        context["page_title"] = "Activity"
        context["selected_page"] = "activity"
        context["main_nav"] = "activity"
        return context


POWER_ACTIONS = {
    "start": ("Start", "start_workspaces", "Starting", "last_start_time"),
    "stop": ("Stop", "stop_workspaces", "Stopping", "last_stop_time"),
    "restart": ("Restart", "reboot_workspaces", "Starting", "last_start_time"),
}


class DesktopSyncView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']
    def post(self, request, *args, **kwargs):
        try:
            desktops = describe_desktops()
        except AlibabaApiError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(reverse("view_dashboards:desktop_list"))

        saved = 0
        for desktop in desktops:
            if apply_desktop(desktop):
                saved += 1
        messages.success(request, "Synced {} desktop(s) from AWS.".format(saved))
        return HttpResponseRedirect(reverse("view_dashboards:desktop_list"))


class DesktopDeleteView(GroupRequiredMixin, DashboardMixin, DeleteView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_delete.html"
    context_object_name = "desktop"
    success_url = reverse_lazy("view_dashboards:desktop_list")

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user", "current_profile")

    def get_context_data(self, **kwargs):
        context = super(DesktopDeleteView, self).get_context_data(**kwargs)
        context["page_title"] = "Delete desktop"
        context["main_nav"] = "desktops"
        return context

    def form_valid(self, form):
        vm = self.object
        removed_from_cloud = bool(vm.vm_id)
        if vm.vm_id:
            try:
                delete_cloud_desktop(vm.vm_id)
            except AlibabaApiError as exc:
                code = (exc.code or "").lower()
                message = str(exc).lower()
                missing = "notfound" in code or "not found" in message or "not exist" in message
                if not missing:
                    messages.error(self.request, str(exc))
                    return HttpResponseRedirect(reverse("view_dashboards:desktop_delete", args=[vm.pk]))
                removed_from_cloud = False
        if removed_from_cloud:
            messages.success(self.request, "Desktop removed from AWS and this dashboard.")
        else:
            messages.success(self.request, "Desktop removed from this dashboard.")
        return super(DesktopDeleteView, self).form_valid(form)


class DesktopPowerView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def post(self, request, pk, action):
        spec = POWER_ACTIONS.get(action)
        vm = get_object_or_404(VirtualMachine, pk=pk)
        detail_url = reverse("view_dashboards:desktop_detail", args=[pk])
        if spec is None:
            messages.error(request, "Unknown desktop action.")
            return HttpResponseRedirect(detail_url)
        try:
            operate_desktop(request, vm, *spec)
        except AlibabaApiError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "{} requested.".format(spec[0]))
        return HttpResponseRedirect(detail_url)


class DesktopConnectView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def post(self, request, pk):
        vm = get_object_or_404(VirtualMachine.objects.select_related("user"), pk=pk)
        detail_url = reverse("view_dashboards:desktop_detail", args=[pk])
        try:
            ticket = connection_ticket(vm)
        except AlibabaApiError as exc:
            write_log(request, vm, "API Error", str(exc))
            messages.error(request, str(exc))
            return HttpResponseRedirect(detail_url)

        content = ticket.encode("utf-8")
        filename = "{}-connect.txt".format(vm.vm_id or "desktop")
        response = HttpResponse(content, content_type="text/plain")
        response["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
        write_log(request, vm, "Connect", "Downloaded WorkSpaces connection details")
        return response