import base64
import logging
from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.views.generic.edit import CreateView, FormView, UpdateView, DeleteView
from django.views.generic.base import RedirectView, TemplateView
from django.views.generic import DetailView, ListView, View

from django.utils import timezone
from django.utils.timezone import localtime
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
from django.contrib.auth.models import User

from apps.profiles.models import Profile
from apps.virtuals.choices import DESKTOP_TYPE_SPECS, spec_summary
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine
from core.mixins import DashboardMixin, GroupRequiredMixin
from view_api.views import (
    AlibabaApiError,
    REGION_ID,
    WUYING_DOWNLOAD_URL,
    WUYING_WEB_URL,
    apply_desktop,
    assign_desktop_user,
    connection_ticket,
    BUSY_STATUSES,
    create_cloud_desktop,
    create_wuying_user,
    delete_cloud_desktop,
    describe_desktops,
    describe_wuying_users,
    change_wuying_password,
    desktop_hourly_price,
    modify_desktop_spec,
    remove_wuying_user,
    operate_desktop,
    write_log,
)
from .forms import (
    DashboardLoginForm,
    DesktopCreateForm,
    DesktopSpecForm,
    DashboardUserCreateForm,
    DashboardUserUpdateForm,
    UserProfileDeleteForm,
    end_user_queryset,
    VMProfileForm,
    VirtualMachineForm,
    WuyingPasswordForm,
)

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
        context['cost'] = desktop_cost_report()
        return context

    def get_template_names(self):
        # Only the superadmin dashboard exists for now; reuse it for any
        # authenticated dashboard user until dedicated staff templates exist.
        self.template_name = "view_dashboards/_group_dashboard/superadmin/dashboard.html"
        return super(DashboardPageView, self).get_template_names()


def ensure_desktop_types():
    missing = VirtualMachine.objects.filter(vm_id__gt="").filter(
        Q(desktop_type="") | Q(desktop_type__isnull=True)
    )
    if not missing.exists():
        return
    try:
        for desktop in describe_desktops():
            apply_desktop(desktop)
    except AlibabaApiError:
        return


RUNNING_ACTIONS = {"Create", "Start", "Restart"}
STOPPED_ACTIONS = {"Stop", "Delete"}


def running_intervals(vm, events, now):
    if not events:
        if vm.status == "Running":
            start = vm.last_start_time or vm.created
            return [(start, now)] if start else []
        if vm.last_start_time and vm.last_stop_time and vm.last_start_time < vm.last_stop_time:
            return [(vm.last_start_time, min(vm.last_stop_time, now))]
        return []

    intervals = []
    opened = None
    for event in events:
        if event["action"] in RUNNING_ACTIONS and opened is None:
            opened = event["created"]
        elif event["action"] in STOPPED_ACTIONS and opened is not None:
            intervals.append((opened, event["created"]))
            opened = None
    if opened is not None:
        intervals.append((opened, now))
    return intervals


def add_daily_cost(daily, start, end, hourly):
    start = localtime(start)
    end = localtime(end)
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        chunk_end = min(end, next_midnight)
        hours = (chunk_end - cursor).total_seconds() / 3600
        daily[cursor.date()] = daily.get(cursor.date(), 0) + hours * hourly
        cursor = chunk_end


def money_label(value):
    if not value:
        return "$0.00"
    if abs(value) < 0.1:
        return "${:.4f}".format(value)
    return "${:.2f}".format(value)


def cost_chart(points):
    width, height = 640, 220
    left, right, top, bottom = 58, 16, 16, 32
    plot_w = width - left - right
    plot_h = height - top - bottom
    actual_peak = max((point["cumulative"] for point in points), default=0)
    peak = actual_peak or 1
    coords = []
    steps = max(len(points) - 1, 1)
    for index, point in enumerate(points):
        x = left + plot_w * (index / steps)
        y = top + plot_h - plot_h * (point["cumulative"] / peak)
        coords.append({
            "x": round(x, 1),
            "y": round(y, 1),
            "label": point["label"],
            "cumulative_label": point["cumulative_label"],
            "show_label": index == 0 or index == steps or (len(points) > 8 and index % 5 == 0),
        })
    if len(coords) == 1:
        only = coords[0]
        coords = [
            dict(only, x=left, show_label=True),
            dict(only, x=round(left + plot_w, 1), show_label=False),
        ]
    line = " ".join("{},{}".format(point["x"], point["y"]) for point in coords)
    baseline = top + plot_h
    area = "{left},{baseline} {line} {end},{baseline}".format(
        left=left,
        baseline=baseline,
        line=line,
        end=coords[-1]["x"] if coords else left,
    )
    return {
        "width": width,
        "height": height,
        "left": left,
        "baseline": baseline,
        "peak_label": money_label(actual_peak),
        "line": line,
        "area": area,
        "coords": coords,
    }


def desktop_cost_report():
    now = timezone.now()
    month_start = localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ensure_desktop_types()
    vms = list(VirtualMachine.objects.select_related("current_profile").order_by("name"))
    events = {}
    if vms:
        for row in ActivityLog.objects.filter(
            virtual_machine_id__in=[vm.id for vm in vms],
            action__in=["Create", "Start", "Stop", "Restart", "Delete"],
        ).order_by("created").values("virtual_machine_id", "action", "created"):
            events.setdefault(row["virtual_machine_id"], []).append(row)

    daily = {}
    rows = []
    unpriced = 0
    for vm in vms:
        quote = quote_desktop(vm)
        if not quote:
            unpriced += 1
            continue
        hours = 0
        for start, end in running_intervals(vm, events.get(vm.id, []), now):
            start = max(start, month_start)
            end = min(end, now)
            if end <= start:
                continue
            hours += (end - start).total_seconds() / 3600
            add_daily_cost(daily, start, end, quote["hourly"])
        rows.append({
            "name": vm.name or "Untitled",
            "pk": vm.pk,
            "hours": hours,
            "cost": hours * quote["hourly"],
            "hours_label": "{:.1f} h".format(hours),
            "cost_label": money_label(hours * quote["hourly"]),
            "hourly_label": money_label(quote["hourly"]),
        })

    points = []
    cumulative = 0
    day = month_start.date()
    today = localtime(now).date()
    while day <= today:
        cumulative += daily.get(day, 0)
        points.append({
            "label": day.strftime("%d/%m/%y"),
            "cumulative": cumulative,
            "cumulative_label": money_label(cumulative),
        })
        day += timedelta(days=1)

    total = points[-1]["cumulative"] if points else 0
    rows.sort(key=lambda row: row["cost"], reverse=True)
    return {
        "month_label": month_start.strftime("%B %Y"),
        "total": total,
        "total_label": money_label(total),
        "points": points,
        "chart": cost_chart(points),
        "rows": rows,
        "unpriced": unpriced,
    }


def quote_desktop(vm):
    desktop_type = vm.desktop_type
    if not desktop_type and vm.current_profile_id:
        desktop_type = vm.current_profile.instance_type_code or ""
    if not desktop_type:
        return None
    try:
        return desktop_hourly_price(desktop_type, vm.system_disk, vm.data_disk)
    except AlibabaApiError:
        return None


class DesktopListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_list.html"
    context_object_name = "desktops"
    paginate_by = 20

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user__profile", "current_profile").order_by("-created")

    def get_context_data(self, **kwargs):
        context = super(DesktopListView, self).get_context_data(**kwargs)
        context["page_title"] = "Desktops"
        context["selected_page"] = "desktops"
        context["main_nav"] = "desktops"
        context["wuying_download_url"] = WUYING_DOWNLOAD_URL
        context["wuying_web_url"] = WUYING_WEB_URL
        context["office_network_id"] = settings.DIRECTORY_ID if settings.DIRECTORY_ID != "xxxx" else ""
        context["busy_statuses"] = BUSY_STATUSES
        ensure_desktop_types()
        monthly_total = 0
        for desktop in context["desktops"]:
            desktop.refresh_from_db()
            desktop.price = quote_desktop(desktop)
            if desktop.price:
                monthly_total += desktop.price["monthly"]
        context["monthly_total"] = monthly_total
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
        ensure_desktop_types()
        self.object.refresh_from_db()
        context["desktop_price"] = quote_desktop(self.object)
        context["desktop_busy"] = self.object.status in BUSY_STATUSES
        context["selected_page"] = "desktops"
        context["main_nav"] = "desktops"
        context["activity_logs"] = self.object.activity_logs.select_related("actor").order_by("-created")[:30]
        return context


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


def log_desktop(request, vm, action, old_value='', new_value='', details='', desktop_name='', cloud_id=''):
    write_log(request, vm, action, details, old_value, new_value, desktop_name, cloud_id)


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
    page_subtitle = 'Purchases a Windows 11 Pro computer in China (Hangzhou). Alibaba bills it pay-as-you-go.'
    cancel_url_name = 'view_dashboards:desktop_list'

    def get_context_data(self, **kwargs):
        context = super(DesktopCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Create cloud computer'
        context['main_nav'] = 'desktops'
        context['submit_label'] = 'Create cloud computer'
        return context

    def form_valid(self, form):
        vm = form.save(commit=False)
        end_user_id = ""
        if vm.user_id:
            profile = Profile.objects.filter(user_id=vm.user_id).first()
            if profile and profile.eds_end_user_id:
                end_user_id = profile.eds_end_user_id
        try:
            created = create_cloud_desktop(vm.name, vm.current_profile, end_user_id)
        except AlibabaApiError as exc:
            log_desktop(
                self.request,
                None,
                "API Error",
                details=str(exc),
                desktop_name=vm.name or "",
            )
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        vm.vm_id = created["vm_id"]
        vm.cpu = created["cpu"]
        vm.ram = created["ram"]
        vm.desktop_type = created["desktop_type"]
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
            "Create",
            new_value=spec_summary(vm.desktop_type, vm.system_disk, vm.data_disk),
            details="Created Windows 11 Pro desktop on Alibaba, pay-as-you-go",
        )
        if vm.user_id:
            log_desktop(
                self.request,
                vm,
                "Assign",
                new_value=str(vm.user),
                details="Assigned when the desktop was created",
            )
        messages.success(
            self.request,
            'Alibaba is creating this Windows 11 Pro desktop. This page updates the status on its own.',
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

        assigned_in_alibaba = False
        if previous.user_id != vm.user_id and vm.vm_id:
            end_user_id = ''
            if vm.user_id:
                profile = Profile.objects.filter(user_id=vm.user_id).first()
                end_user_id = profile.eds_end_user_id if profile else ''
                if not end_user_id:
                    form.add_error('user', 'This user has no Wuying username. Add one on the Users page first.')
                    return self.form_invalid(form)
            try:
                assign_desktop_user(vm.vm_id, end_user_id)
            except AlibabaApiError as exc:
                log_desktop(self.request, vm, "API Error", details=str(exc))
                form.add_error(None, 'Alibaba could not change the user: {} The computer must be Running.'.format(exc))
                return self.form_invalid(form)
            assigned_in_alibaba = True

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
            action = "Update"
            old_value = ''
            new_value = ''
            details = 'Updated desktop'

        log_desktop(self.request, vm, action, old_value, new_value, details)
        if assigned_in_alibaba and vm.user_id:
            messages.success(self.request, 'Desktop assigned to {} in Alibaba. They will see it after signing in.'.format(vm.user))
        elif assigned_in_alibaba:
            messages.success(self.request, 'Desktop unassigned in Alibaba.')
        else:
            messages.success(self.request, 'Desktop updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:desktop_detail', args=[self.object.pk])


class DesktopSpecView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = DesktopSpecForm
    cancel_url_name = 'view_dashboards:desktop_list'

    def dispatch(self, request, *args, **kwargs):
        self.desktop = get_object_or_404(VirtualMachine, pk=kwargs['pk'])
        return super(DesktopSpecView, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(DesktopSpecView, self).get_form_kwargs()
        kwargs['desktop'] = self.desktop
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(DesktopSpecView, self).get_context_data(**kwargs)
        context['page_title'] = 'Change specifications'
        context['main_nav'] = 'desktops'
        context['submit_label'] = 'Change specifications'
        context['cancel_url'] = reverse('view_dashboards:desktop_detail', args=[self.desktop.pk])
        rules = (
            'The computer must be Stopped. CPU, memory, and GPU change together, and only inside the same family. '
            'Disks can only grow. Windows and files stay on the computer. Wait 5 minutes between changes.'
        )
        if self.desktop.status != 'Stopped':
            rules = 'This computer is {}. Stop it before changing specifications. {}'.format(
                self.desktop.get_status_display(), rules,
            )
        context['page_subtitle'] = rules
        return context

    def form_valid(self, form):
        desktop = self.desktop
        type_id = form.cleaned_data['desktop_type']
        system_disk = form.cleaned_data['system_disk']
        data_disk = form.cleaned_data['data_disk']
        old_value = spec_summary(desktop.desktop_type, desktop.system_disk, desktop.data_disk)
        try:
            modify_desktop_spec(
                desktop.vm_id,
                type_id,
                system_disk if system_disk != desktop.system_disk else None,
                data_disk if data_disk != desktop.data_disk else None,
            )
        except AlibabaApiError as exc:
            log_desktop(self.request, desktop, 'API Error', details=str(exc))
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        spec = DESKTOP_TYPE_SPECS.get(type_id) or {}
        desktop.desktop_type = type_id
        desktop.cpu = spec.get('cpu', desktop.cpu)
        desktop.ram = spec.get('ram', desktop.ram)
        desktop.system_disk = system_disk
        desktop.data_disk = data_disk
        desktop.current_profile = VMProfile.objects.filter(
            instance_type_code=type_id,
            system_disk=system_disk,
            data_disk=data_disk,
        ).first()
        desktop.status = 'Updating'
        desktop.modified_by = self.request.user
        desktop.save()
        log_desktop(
            self.request,
            desktop,
            'Resize',
            old_value=old_value,
            new_value=spec_summary(type_id, system_disk, data_disk),
            details='Changed specifications on Alibaba. The computer stays stopped until you start it.',
        )
        messages.success(
            self.request,
            'Alibaba is changing this computer. Files and Windows stay. Start it again when the status leaves Updating.',
        )
        return HttpResponseRedirect(reverse('view_dashboards:desktop_detail', args=[desktop.pk]))


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
        log_desktop(
            self.request, None, "Create",
            new_value=spec_summary(profile.instance_type_code, profile.system_disk, profile.data_disk),
            details="Saved a hardware profile.",
            desktop_name=profile.name or "VM profile",
        )
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
        log_desktop(
            self.request, None, "Update",
            new_value=spec_summary(profile.instance_type_code, profile.system_disk, profile.data_disk),
            details="Updated a hardware profile.",
            desktop_name=profile.name or "VM profile",
        )
        messages.success(self.request, 'VM profile updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:vm_profile_list')


class UserProfileListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = User
    template_name = 'view_dashboards/_group_dashboard/superadmin/user_profile_list.html'
    context_object_name = 'accounts'
    paginate_by = 20

    def get_queryset(self):
        return end_user_queryset().select_related('profile')

    def get_context_data(self, **kwargs):
        context = super(UserProfileListView, self).get_context_data(**kwargs)
        context['page_title'] = 'Users'
        context['main_nav'] = 'users'

        wuying_users = {}
        context['wuying_error'] = ''
        try:
            wuying_users = describe_wuying_users()
        except AlibabaApiError as exc:
            context['wuying_error'] = str(exc)

        for account in context['accounts']:
            try:
                profile = account.profile
            except Profile.DoesNotExist:
                profile = None
            end_user_id = profile.eds_end_user_id if profile else ''
            account.wuying = wuying_users.get(end_user_id or '')
        linked = set(Profile.objects.exclude(eds_end_user_id__isnull=True).exclude(eds_end_user_id='').values_list('eds_end_user_id', flat=True))
        context['unlinked_wuying_users'] = [
            user for end_user_id, user in sorted(wuying_users.items()) if end_user_id not in linked
        ]
        return context


class UserProfileCreateView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = DashboardUserCreateForm
    page_subtitle = 'Creates the dashboard account here, then the Wuying login in Alibaba. The admin site is not required.'
    cancel_url_name = 'view_dashboards:user_profile_list'

    def get_context_data(self, **kwargs):
        context = super(UserProfileCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Add user'
        context['main_nav'] = 'users'
        context['submit_label'] = 'Create user'
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        user = User.objects.create_user(
            username=data['username'],
            email=data.get('email') or '',
            password=data['password'],
            first_name=data.get('first_name') or '',
            last_name=data.get('last_name') or '',
        )
        end_user_id = data['eds_end_user_id']
        try:
            if data['wuying_mode'] == 'create':
                create_wuying_user(
                    end_user_id,
                    data['email'],
                    data['wuying_password'],
                    data.get('display_name') or user.get_full_name(),
                )
            elif end_user_id not in describe_wuying_users():
                user.delete()
                form.add_error('eds_end_user_id', 'Alibaba has no Wuying user with this username.')
                return self.form_invalid(form)
        except AlibabaApiError as exc:
            user.delete()
            log_desktop(self.request, None, "API Error", details=str(exc), desktop_name=user.username)
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        profile = Profile(user=user, eds_end_user_id=end_user_id, created_by=self.request.user)
        if data['wuying_mode'] == 'create':
            profile.wuying_password = data['wuying_password']
        profile.save()
        if data['wuying_mode'] == 'create':
            log_desktop(
                self.request, None, "Create user",
                new_value=end_user_id,
                details="Created the dashboard account and the Wuying user in Alibaba.",
                desktop_name=user.username,
            )
            messages.success(self.request, 'User created. They can sign in to this dashboard and to the Wuying app.')
        else:
            log_desktop(
                self.request, None, "Create user",
                new_value=end_user_id,
                details="Created the dashboard account and linked an existing Wuying user.",
                desktop_name=user.username,
            )
            messages.success(self.request, 'User created and linked to the existing Wuying login.')
        return HttpResponseRedirect(reverse('view_dashboards:user_profile_list'))


class UserProfileUpdateView(GroupRequiredMixin, DashboardFormMixin, UpdateView):
    group_required = ['superadmin']
    model = User
    form_class = DashboardUserUpdateForm

    def get_queryset(self):
        return end_user_queryset()

    page_subtitle = 'Update this dashboard account. Leave the password blank to keep it.'
    cancel_url_name = 'view_dashboards:user_profile_list'

    def get_context_data(self, **kwargs):
        context = super(UserProfileUpdateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Edit user'
        context['main_nav'] = 'users'
        return context

    def form_valid(self, form):
        user = form.save()
        self.object = user
        profile = Profile.objects.filter(user=user).first()
        if profile:
            profile.modified_by = self.request.user
            profile.save(update_fields=['modified_by', 'modified'])
        log_desktop(
            self.request, None, "Update",
            new_value=profile.eds_end_user_id if profile else "",
            details="Updated the dashboard account.",
            desktop_name=user.username,
        )
        messages.success(self.request, 'User updated.')
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('view_dashboards:user_profile_list')


class UserProfilePasswordView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = WuyingPasswordForm
    cancel_url_name = 'view_dashboards:user_profile_list'

    def dispatch(self, request, *args, **kwargs):
        self.account = get_object_or_404(end_user_queryset(), pk=kwargs['pk'])
        self.profile = get_object_or_404(Profile.objects.select_related('user'), user=self.account)
        return super(UserProfilePasswordView, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(UserProfilePasswordView, self).get_context_data(**kwargs)
        context['page_title'] = 'Set Wuying password'
        context['page_subtitle'] = 'Sets a new Wuying password for {} ({}) in Alibaba.'.format(
            self.profile.user.username,
            self.profile.eds_end_user_id or 'not linked',
        )
        context['main_nav'] = 'users'
        context['submit_label'] = 'Set password'
        return context

    def form_valid(self, form):
        if not self.profile.eds_end_user_id:
            form.add_error(None, 'This user has no Wuying username. Edit the profile first.')
            return self.form_invalid(form)
        try:
            change_wuying_password(self.profile.eds_end_user_id, form.cleaned_data['new_password'])
        except AlibabaApiError as exc:
            log_desktop(
                self.request, None, "API Error", details=str(exc),
                desktop_name=self.profile.user.username,
            )
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        self.profile.wuying_password = form.cleaned_data['new_password']
        self.profile.save(update_fields=['wuying_password'])
        log_desktop(
            self.request, None, "Set password",
            details="Set a new Wuying password for {}.".format(self.profile.eds_end_user_id),
            desktop_name=self.profile.user.username,
        )
        messages.success(
            self.request,
            'New Wuying password set for {}. It is shown on the Users page.'.format(self.profile.eds_end_user_id),
        )
        return HttpResponseRedirect(reverse('view_dashboards:user_profile_list'))


class UserProfileDeleteView(GroupRequiredMixin, DashboardMixin, DeleteView):
    group_required = ['superadmin']
    model = User
    form_class = UserProfileDeleteForm

    def get_queryset(self):
        return end_user_queryset()

    template_name = 'view_dashboards/_group_dashboard/superadmin/user_profile_delete.html'
    context_object_name = 'account'
    success_url = reverse_lazy('view_dashboards:user_profile_list')

    def dispatch(self, request, *args, **kwargs):
        if str(kwargs.get('pk')) == str(request.user.pk):
            messages.error(request, 'You cannot delete the account you are signed in with.')
            return HttpResponseRedirect(reverse('view_dashboards:user_profile_list'))

        return super(UserProfileDeleteView, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(UserProfileDeleteView, self).get_context_data(**kwargs)
        context['page_title'] = 'Delete user'
        context['main_nav'] = 'users'
        context['profile'] = Profile.objects.filter(user=self.object).first()
        context['assigned_desktops'] = VirtualMachine.objects.filter(user=self.object).order_by('name')

        return context

    def form_valid(self, form):
        profile = Profile.objects.filter(user=self.object).first()
        end_user_id = profile.eds_end_user_id if profile else ''
        remove = form.cleaned_data['wuying_action'] == 'remove' and end_user_id
        if remove:
            try:
                remove_wuying_user(end_user_id)
            except AlibabaApiError as exc:
                log_desktop(
                    self.request, None, "API Error", details=str(exc),
                    desktop_name=self.object.username,
                )
                form.add_error(None, str(exc))
                return self.form_invalid(form)

        log_desktop(
            self.request, None, "Delete user",
            old_value=end_user_id or "",
            details="Deleted the dashboard account and the Wuying user in Alibaba." if remove else "Deleted the dashboard account. The Wuying user is still in Alibaba.",
            desktop_name=self.object.username,
        )

        if remove:
            messages.success(self.request, 'User deleted here and in Alibaba. Assigned desktops are now unassigned.')
        else:
            messages.success(self.request, 'User deleted. Assigned desktops are now unassigned.')

        return super(UserProfileDeleteView, self).form_valid(form)

class GuidanceView(GroupRequiredMixin, DashboardMixin, TemplateView):
    group_required = ['superadmin']
    template_name = "view_dashboards/_group_dashboard/superadmin/guidance.html"

    def get_context_data(self, **kwargs):
        context = super(GuidanceView, self).get_context_data(**kwargs)
        context["page_title"] = "Guidance"
        context["main_nav"] = "guidance"
        context["wuying_download_url"] = WUYING_DOWNLOAD_URL
        context["wuying_web_url"] = WUYING_WEB_URL
        directory_id = settings.DIRECTORY_ID
        context["office_network_id"] = directory_id if directory_id and directory_id != "xxxx" else ""

        return context


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
    "start": ("Start", "StartDesktops", "Starting", "last_start_time"),
    "stop": ("Stop", "StopDesktops", "Stopping", "last_stop_time"),
    "restart": ("Restart", "RebootDesktops", "Starting", "last_start_time"),
}


class DesktopProgressView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def get(self, request, *args, **kwargs):
        busy = VirtualMachine.objects.filter(status__in=BUSY_STATUSES, vm_id__gt="").exists()
        if busy:
            try:
                for desktop in describe_desktops():
                    apply_desktop(desktop)
            except AlibabaApiError as exc:
                return JsonResponse({"detail": str(exc)}, status=502)
        rows = []
        for vm in VirtualMachine.objects.all():
            rows.append({
                "id": vm.pk,
                "status": vm.status,
                "label": vm.get_status_display(),
                "busy": vm.status in BUSY_STATUSES,
            })

        return JsonResponse({"desktops": rows})

class DesktopSyncView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']
    def post(self, request, *args, **kwargs):
        try:
            desktops = describe_desktops()
        except AlibabaApiError as exc:
            log_desktop(request, None, "API Error", details=str(exc), desktop_name="All desktops")
            messages.error(request, str(exc))
            return HttpResponseRedirect(reverse("view_dashboards:desktop_list"))

        saved = 0
        for desktop in desktops:
            if apply_desktop(desktop):
                saved += 1
        log_desktop(
            request, None, "Sync",
            details="Synced {} desktop(s) from Alibaba.".format(saved),
            desktop_name="All desktops",
        )
        messages.success(request, "Synced {} desktop(s) from Alibaba.".format(saved))
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
                    log_desktop(self.request, vm, "API Error", details=str(exc))
                    messages.error(self.request, str(exc))
                    return HttpResponseRedirect(reverse("view_dashboards:desktop_delete", args=[vm.pk]))
                removed_from_cloud = False
        if removed_from_cloud:
            details = "Released the computer on Alibaba and removed it from the dashboard."
            messages.success(self.request, "Desktop removed from Alibaba and this dashboard.")
        else:
            details = "Removed the dashboard record."
            messages.success(self.request, "Desktop removed from this dashboard.")
        log_desktop(
            self.request,
            vm,
            "Delete",
            old_value=spec_summary(vm.desktop_type, vm.system_disk, vm.data_disk),
            details=details,
        )
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

        try:
            content = base64.b64decode(ticket, validate=True)
        except (ValueError, TypeError):
            content = ticket.encode("utf-8")
        filename = "{}.ica".format(vm.vm_id or "desktop")
        response = HttpResponse(content, content_type="application/octet-stream")
        response["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
        write_log(request, vm, "Connect", "Downloaded connection ticket")
        return response