import base64
import logging
import re
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.conf import settings
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.models import User

from apps.profiles.models import Profile
from apps.virtuals.choices import DESKTOP_TYPE_SPECS, desktop_type_label, spec_summary
from apps.virtuals.models import ActivityLog, CloudRegion, OfficeNetwork, VMProfile, VirtualMachine
from apps.virtuals.screen_summary import parse_date_range, pending_count, recording_rows, summarize_next, work_notes
from core.mixins import DashboardMixin, GroupRequiredMixin, WorkerRequiredMixin, is_worker
from view_api.views import (
    AlibabaApiError,
    WUYING_DOWNLOAD_URL,
    WUYING_WEB_URL,
    apply_desktop,
    assign_desktop_user,
    connection_ticket,
    desktop_auth_code,
    BUSY_STATUSES,
    create_cloud_desktop,
    create_wuying_user,
    delete_cloud_desktop,
    describe_desktops,
    describe_recordings,
    recording_signed_url,
    refresh_desktop,
    mark_desktop_released,
    describe_signin_events,
    describe_policy,
    describe_policy_groups,
    fetch_desktop,
    policy_is_shared,
    save_desktop_websites,
    set_website_access,
    lock_browser_secure_dns,
    desktop_policy_id_from,
    website_access,
    describe_wuying_users,
    default_network,
    enabled_networks,
    in_region,
    sync_region,
    sync_region_list,
    parse_alibaba_time,
    SIGNIN_EVENT_LABELS,
    change_wuying_password,
    desktop_hourly_price,
    bandwidth_state,
    modify_desktop_spec,
    set_office_bandwidth,
    remove_wuying_user,
    operate_desktop,
    write_log,
)
from .forms import (
    OfficeNetworkForm,
    DesktopWebsiteForm,
    RegionPolicyForm,
    WebsiteRuleFormSet,
    display_websites,
    expand_websites,
    website_domains,
    RegionSelectForm,
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
            self.is_worker = is_worker(user)

            if self.is_superadmin or self.is_staff or self.is_worker:
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
        if getattr(self, 'is_worker', False):
            return reverse('view_dashboards:my_computer')

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
        return "USD 0.00"
    if abs(value) < 0.1:
        return "USD {:.4f}".format(value)
    return "USD {:.2f}".format(value)


def same_settlement_hour(start, end):
    # Alibaba settles pay-as-you-go on the hour, UTC+8.
    start = localtime(start)
    end = localtime(end)
    return (
        start.year == end.year
        and start.month == end.month
        and start.day == end.day
        and start.hour == end.hour
    )


def hours_inside(start, end, window_start, window_end):
    start = max(start, window_start)
    end = min(end, window_end)
    if end <= start:
        return 0
    return (end - start).total_seconds() / 3600


def gaps_between(exist_start, exist_end, running):
    cursor = exist_start
    gaps = []
    for start, end in running:
        start = max(start, exist_start)
        end = min(end, exist_end)
        if end <= start:
            continue
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < exist_end:
        gaps.append((cursor, exist_end))
    return gaps


def spec_from_summary(text):
    parts = [part.strip() for part in (text or "").split(" · ")]
    if len(parts) != 3:
        return None
    label = parts[0]
    system_text = parts[1].replace("system", "").replace("GiB", "").strip()
    data_text = parts[2].replace("data", "").replace("GiB", "").strip()
    type_id = ""
    for candidate in DESKTOP_TYPE_SPECS:
        if desktop_type_label(candidate) == label:
            type_id = candidate
            break
    if not type_id:
        return None
    try:
        return type_id, int(system_text), int(data_text)
    except ValueError:
        return None


def desktop_life(logs, vm, now):
    spec = None
    exist_start = None
    exist_end = None
    opened = None
    running = []
    name = ""
    region = ""
    for log in logs:
        if log.desktop_name:
            name = log.desktop_name
        if log.region:
            region = log.region
        if log.action in ("Create", "Resize"):
            parsed = spec_from_summary(log.new_value)
            if parsed:
                spec = parsed
        if log.action == "Create" and exist_start is None:
            exist_start = log.created
        elif log.action in ("Start", "Restart") and opened is None:
            opened = log.created
        elif log.action == "Stop":
            if opened is not None:
                running.append((opened, log.created))
                opened = None
            elif not running and exist_start and exist_start < log.created:
                # A new computer is already running. Stop with no Start
                # closes that first run at the click, not at the last visit.
                running.append((exist_start, log.created))
        elif log.action == "Delete" and exist_end is None:
            if opened is not None:
                running.append((opened, log.created))
                opened = None
            exist_end = log.created
    if vm is not None and vm.region:
        region = vm.region
    if vm is not None and vm.desktop_type:
        spec = (vm.desktop_type, vm.system_disk or 0, vm.data_disk or 0)
        name = vm.name or name
    if vm is not None and exist_end is None:
        if exist_start is None:
            exist_start = vm.created
        exist_end = now
        if opened is not None:
            running.append((opened, now))
        elif vm.status in ("Running", "Starting"):
            boot = exist_start
            if vm.last_start_time and exist_start and vm.last_start_time >= exist_start:
                boot = vm.last_start_time
            if boot and (not running or boot >= running[-1][1]) and boot < now:
                running.append((boot, now))
    return {
        "name": name or "Untitled",
        "region": region,
        "spec": spec,
        "exist_start": exist_start,
        "exist_end": exist_end,
        "running": running,
        "vm": vm,
    }


# A visit more than this long after the previous one starts a new run.
# Shorter gaps stay on the full hourly price, because the computer was
# still running between those visits.
RUNNING_GAP = timedelta(hours=3)


def visit_windows(exist_start, exist_end, pairs):
    windows = []
    cluster_start = None
    cluster_end = None
    for connect, disconnect in sorted(pairs):
        connect = max(connect, exist_start)
        disconnect = min(disconnect, exist_end)
        if disconnect <= connect:
            continue
        if cluster_end is None:
            if exist_start <= connect and connect - exist_start <= RUNNING_GAP:
                cluster_start = exist_start
            else:
                cluster_start = connect
            cluster_end = disconnect
            continue
        if connect - cluster_end <= RUNNING_GAP:
            cluster_end = max(cluster_end, disconnect)
        else:
            windows.append((cluster_start, cluster_end))
            cluster_start = connect
            cluster_end = disconnect
    if cluster_end is not None:
        windows.append((cluster_start, cluster_end))
    return windows


def usage_windows(life, pairs, now):
    if life["running"]:
        return life["running"]
    start = life["exist_start"]
    if not start:
        return []
    end = life["exist_end"] or now
    vm = life["vm"]
    if vm is not None and vm.status in ("Running", "Starting"):
        boot = start
        if vm.last_start_time and vm.last_start_time >= start:
            boot = vm.last_start_time
        return [(boot, now)]
    return visit_windows(start, end, pairs or [])


def cost_chart(points):
    width, height = 1200, 300
    left, right, top, bottom = 108, 16, 16, 48
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
            "label_x": round(x, 1),
            "label": point["label"],
            "anchor": "middle",
            "cumulative_label": point["cumulative_label"],
            "show_label": index == 0 or index == steps or (len(points) > 6 and index % 5 == 0),
        })
    if len(coords) == 1:
        only = coords[0]
        mid = round(left + plot_w / 2, 1)
        coords = [
            dict(only, x=left, label_x=mid, anchor="middle", show_label=True),
            dict(only, x=round(left + plot_w, 1), show_label=False),
        ]
    elif coords:
        coords[0]["anchor"] = "start"
        coords[-1]["anchor"] = "end"
    line = " ".join("{},{}".format(point["x"], point["y"]) for point in coords)
    baseline = round(top + plot_h, 1)
    area = "{left},{baseline} {line} {end},{baseline}".format(
        left=left,
        baseline=baseline,
        line=line,
        end=coords[-1]["x"] if coords else left,
    )
    tick_count = 4 if actual_peak else 0
    ticks = []
    for step in range(tick_count + 1):
        fraction = 0 if tick_count == 0 else step / tick_count
        value = actual_peak * fraction
        label = money_label(value)
        box_width = max(52, len(label) * 6.6 + 14)
        y = round(top + plot_h - plot_h * fraction, 1)
        ticks.append({
            "y": y,
            "label": label,
            "accent": step == tick_count and actual_peak > 0,
            "box_width": round(box_width, 1),
            "box_x": round(left - 10 - box_width, 1),
            "label_x": left - 14,
        })
    return {
        "width": width,
        "height": height,
        "left": left,
        "right": width - right,
        "top": top,
        "baseline": baseline,
        "y_title_x": 18,
        "y_title_y": round(top + plot_h / 2, 1),
        "x_title_x": round(left + plot_w / 2, 1),
        "x_title_y": height - 8,
        "line": line,
        "area": area,
        "coords": coords,
        "ticks": ticks,
        "last": coords[-1] if coords else None,
    }


def desktop_cost_report():
    now = timezone.now()
    month_start = localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ensure_desktop_types()
    current = {
        vm.vm_id: vm
        for vm in VirtualMachine.objects.select_related("current_profile").exclude(vm_id="").exclude(vm_id__isnull=True)
    }
    grouped = {}
    for log in ActivityLog.objects.filter(
        action__in=["Create", "Start", "Stop", "Restart", "Delete", "Resize"],
    ).exclude(cloud_id="").order_by("created"):
        grouped.setdefault(log.cloud_id, []).append(log)
    for vm_id in current:
        grouped.setdefault(vm_id, [])

    visits = {}
    open_visit = {}
    try:
        timed = []
        for event in describe_signin_events(month_start, now):
            kind = event.get("EventType")
            desktop_id = event.get("DesktopId") or ""
            when = parse_alibaba_time(event.get("EventTime"))
            if kind not in ("DESKTOP_CONNECT", "DESKTOP_DISCONNECT") or not desktop_id or not when:
                continue
            timed.append((when, kind, desktop_id))
        for when, kind, desktop_id in sorted(timed):
            if kind == "DESKTOP_CONNECT":
                open_visit[desktop_id] = when
                continue
            started = open_visit.pop(desktop_id, None)
            if started and when > started:
                visits.setdefault(desktop_id, []).append((started, when))
    except AlibabaApiError:
        visits = {}

    daily = {}
    rows = []
    unpriced = 0
    remaining = 0
    for cloud_id, logs in grouped.items():
        life = desktop_life(logs, current.get(cloud_id), now)
        life["running"] = usage_windows(life, visits.get(cloud_id), now)
        if not life["exist_start"] or not life["exist_end"]:
            continue
        # A computer released inside the hour it was created does not
        # appear in the posted month total.
        if life["vm"] is None and same_settlement_hour(life["exist_start"], life["exist_end"]):
            continue
        spec = life["spec"]
        quote = None
        if spec:
            try:
                quote = desktop_hourly_price(spec[0], spec[1], spec[2], life["region"] or None)
            except AlibabaApiError:
                quote = None
        if not quote:
            unpriced += 1
            continue
        hourly = quote["hourly"]
        disk_hourly = quote.get("disk_hourly") or 0
        on_hours = 0
        for start, end in life["running"]:
            start = max(start, life["exist_start"])
            end = min(end, life["exist_end"])
            on_hours += hours_inside(start, end, month_start, now)
            add_daily_cost(daily, max(start, month_start), min(end, now), hourly)
        off_hours = 0
        for start, end in gaps_between(life["exist_start"], life["exist_end"], life["running"]):
            off_hours += hours_inside(start, end, month_start, now)
            add_daily_cost(daily, max(start, month_start), min(end, now), disk_hourly)
        used = on_hours * hourly + off_hours * disk_hourly
        vm = life["vm"]
        rows.append({
            "name": life["name"],
            "pk": vm.pk if vm is not None else None,
            "hours": on_hours,
            "hours_label": "{:.1f} h".format(on_hours),
            "disk_hours": off_hours,
            "disk_hours_label": "{:.1f} h".format(off_hours),
            "hourly_label": money_label(hourly),
            "cost": used,
            "cost_label": money_label(used),
            "used_label": money_label(used),
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
        "remaining": remaining,
        "remaining_label": money_label(remaining),
        "expected": total,
        "expected_label": money_label(total),
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
        return desktop_hourly_price(desktop_type, vm.system_disk, vm.data_disk, vm.region or None)
    except AlibabaApiError:
        return None


def region_names():
    return dict(CloudRegion.objects.values_list("region_id", "name"))


def network_banner(networks):
    """The first office network whose bandwidth plan is still updating, for the progress bar."""
    for network in networks:
        try:
            state = bandwidth_state(network)
        except AlibabaApiError:
            continue
        if state.get("updating"):
            return {
                "network": network,
                "status": state.get("status") or "Updating",
                "label": state.get("label") or "",
            }
    return None


class DesktopListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_list.html"
    context_object_name = "desktops"
    paginate_by = 20

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user__profile", "current_profile").order_by("-created")

    def get(self, request, *args, **kwargs):
        try:
            for desktop in describe_desktops():
                apply_desktop(desktop)
        except AlibabaApiError:
            pass
        return super(DesktopListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(DesktopListView, self).get_context_data(**kwargs)
        context["page_title"] = "Desktops"
        context["selected_page"] = "desktops"
        context["main_nav"] = "desktops"
        context["wuying_download_url"] = WUYING_DOWNLOAD_URL
        context["wuying_web_url"] = WUYING_WEB_URL
        context["busy_statuses"] = BUSY_STATUSES
        ensure_desktop_types()
        names = region_names()
        networks = {network.directory_id: network for network in OfficeNetwork.objects.select_related("region")}
        monthly_total = 0
        for desktop in context["desktops"]:
            desktop.refresh_from_db()
            desktop.price = quote_desktop(desktop)
            desktop.region_label = names.get(desktop.region) or desktop.region
            network = networks.get(desktop.directory_id)
            desktop.network_label = network.display_name if network else ""
            if desktop.price:
                monthly_total += desktop.price["monthly"]
        context["monthly_total"] = monthly_total
        context["network_banner"] = network_banner(enabled_networks())
        return context


class DesktopDetailView(GroupRequiredMixin, DashboardMixin, DetailView):
    group_required = ['superadmin']
    model = VirtualMachine
    template_name = "view_dashboards/_group_dashboard/superadmin/desktop_detail.html"
    context_object_name = "desktop"

    def get_queryset(self):
        return VirtualMachine.objects.select_related("user", "current_profile")

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.vm_id:
            try:
                refreshed = refresh_desktop(self.object)
            except AlibabaApiError as exc:
                messages.warning(request, "Could not refresh the status from Alibaba: {}".format(exc))
            else:
                if refreshed is None:
                    messages.info(request, "This computer was released on Alibaba, so it has been removed from the dashboard.")
                    return HttpResponseRedirect(reverse("view_dashboards:desktop_list"))
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

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
        office = OfficeNetwork.objects.select_related("region").filter(directory_id=self.object.directory_id).first()
        context["office_network"] = office
        context["region_label"] = region_names().get(self.object.region) or self.object.region
        network = {}
        if office is not None:
            try:
                network = bandwidth_state(office)
            except AlibabaApiError:
                network = {}
        context["bandwidth_label"] = network.get("label") or ""
        context["network_updating"] = bool(network.get("updating"))
        context["network_status"] = network.get("status") or "Updating"
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


def log_desktop(request, vm, action, old_value='', new_value='', details='', desktop_name='', cloud_id='', region=''):
    write_log(request, vm, action, details, old_value, new_value, desktop_name, cloud_id, region)


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
    page_subtitle = 'Purchases a Windows 11 Pro computer in the office network you pick. Alibaba bills it pay-as-you-go.'
    cancel_url_name = 'view_dashboards:desktop_list'

    def get_context_data(self, **kwargs):
        context = super(DesktopCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Create cloud computer'
        context['main_nav'] = 'desktops'
        context['submit_label'] = 'Create cloud computer'
        context['page_subtitle'] = (
            'Purchases a Windows 11 Pro computer in the office network you pick, with that region\'s recording policy. '
            'Alibaba bills it pay-as-you-go.'
        )
        context['profile_networks'] = {
            str(pk): str(network_id or "")
            for pk, network_id in VMProfile.objects.values_list("pk", "office_network_id")
        }
        return context

    def form_valid(self, form):
        vm = form.save(commit=False)
        end_user_id = ""
        if vm.user_id:
            profile = Profile.objects.filter(user_id=vm.user_id).first()
            if profile and profile.eds_end_user_id:
                end_user_id = profile.eds_end_user_id
        try:
            created = create_cloud_desktop(
                vm.name,
                vm.current_profile,
                end_user_id,
                form.cleaned_data.get("office_network"),
            )
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
        vm.region = created["region"]
        vm.directory_id = created["directory_id"]
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
                assign_desktop_user(vm.vm_id, end_user_id, vm.region or None)
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


class OfficeNetworkView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = OfficeNetworkForm
    cancel_url_name = 'view_dashboards:dashboard'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.groups.filter(name__in=self.group_required).exists():
            raise PermissionDenied
        self.networks = enabled_networks()
        pk = kwargs.get("pk")
        if pk is None:
            if not self.networks:
                messages.warning(request, "No office network is enabled yet. Turn on a region and sync it first.")
                return HttpResponseRedirect(reverse("view_dashboards:region_list"))
            return HttpResponseRedirect(reverse("view_dashboards:office_network_detail", args=[default_network().pk]))
        self.network = get_object_or_404(OfficeNetwork.objects.select_related("region"), pk=pk, region__enabled=True)
        try:
            self.bandwidth = bandwidth_state(self.network)
            self.bandwidth_error = ""
        except AlibabaApiError as exc:
            self.bandwidth = {"mode": "basic", "mbps": 10, "charge": "PayByTraffic", "label": "No premium"}
            self.bandwidth_error = str(exc)
        return super(OfficeNetworkView, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(OfficeNetworkView, self).get_form_kwargs()
        kwargs["bandwidth"] = self.bandwidth
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(OfficeNetworkView, self).get_context_data(**kwargs)
        context["page_title"] = "Network"
        context["main_nav"] = "network"
        context["submit_label"] = "Save network"
        context["page_subtitle"] = (
            "Now {}. This is the internet for every computer in the office network {}. "
            "A computer does not have its own peak. Choosing no premium removes the plan, and the computers lose internet."
        ).format(self.bandwidth["label"], self.network)
        sharing = (
            VirtualMachine.objects.exclude(vm_id="").exclude(vm_id__isnull=True)
            .filter(directory_id=self.network.directory_id).count()
        )
        peak = self.bandwidth.get("mbps") or 10
        context["network_choices"] = [
            {
                "label": str(network),
                "url": reverse("view_dashboards:office_network_detail", args=[network.pk]),
                "active": network.pk == self.network.pk,
            }
            for network in self.networks
        ]
        context["network_info"] = {
            "name": str(self.network),
            "label": self.bandwidth["label"],
            "premium": self.bandwidth.get("mode") == "premium",
            "computers": sharing,
            "peak": peak,
            "half": round(peak / 2, 1),
        }
        if self.bandwidth.get("updating"):
            context["form_locked"] = True
            context["page_subtitle"] = (
                "The office network is updating. Save stays off until Alibaba finishes, so a second change does not fail."
            )
            context["update_progress"] = {
                "title": "Updating network",
                "label": self.bandwidth.get("status") or "Updating",
                "detail": "Every computer in this office network uses this plan. Wait until the bar finishes before saving again.",
                "poll_url": reverse("view_dashboards:office_network_progress", args=[self.network.pk]),
                "poll_mode": "network",
            }
        return context

    def get(self, request, *args, **kwargs):
        if self.bandwidth_error:
            messages.warning(request, "Could not read the current bandwidth from Alibaba: {}".format(self.bandwidth_error))
        return super(OfficeNetworkView, self).get(request, *args, **kwargs)

    def form_valid(self, form):
        old_label = self.bandwidth["label"]
        try:
            new_label = set_office_bandwidth(
                self.network,
                form.cleaned_data["bandwidth_mode"],
                form.cleaned_data["bandwidth_mbps"],
                form.cleaned_data["internet_charge_type"],
            )
        except AlibabaApiError as exc:
            log_desktop(
                self.request, None, "API Error", details=str(exc),
                desktop_name="Office network {}".format(self.network),
                region=self.network.region.region_id,
            )
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        log_desktop(
            self.request,
            None,
            "Update",
            old_value=old_label,
            new_value=new_label,
            details="Changed the office network bandwidth. Every computer in it uses it.",
            desktop_name="Office network {}".format(self.network),
            region=self.network.region.region_id,
        )
        messages.success(self.request, "Network is now {}. The progress bar stays until Alibaba finishes the update.".format(new_label))
        return HttpResponseRedirect(reverse("view_dashboards:office_network_detail", args=[self.network.pk]))


class OfficeNetworkProgressView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def get(self, request, *args, **kwargs):
        network = get_object_or_404(OfficeNetwork.objects.select_related("region"), pk=kwargs["pk"])
        try:
            state = bandwidth_state(network, clear_settled=True)
        except AlibabaApiError as exc:
            return JsonResponse({"detail": str(exc)}, status=502)
        return JsonResponse({
            "updating": bool(state.get("updating")),
            "label": state.get("status") or "Updating",
            "text": state.get("label") or "",
        })


class RegionListView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = RegionSelectForm
    template_name = 'view_dashboards/_group_dashboard/superadmin/region_list.html'
    cancel_url_name = 'view_dashboards:dashboard'

    def get(self, request, *args, **kwargs):
        if CloudRegion.objects.count() < 2:
            try:
                sync_region_list()
            except AlibabaApiError as exc:
                messages.warning(request, "Could not load the region list from Alibaba: {}".format(exc))
        return super(RegionListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(RegionListView, self).get_context_data(**kwargs)
        context['page_title'] = 'Regions'
        context['main_nav'] = 'regions'
        rows = []
        for region in CloudRegion.objects.filter(enabled=True).order_by('name', 'region_id'):
            networks = list(region.office_networks.exclude(status='Missing').order_by('name', 'directory_id'))
            rows.append({
                'region': region,
                'networks': networks,
                'computers': VirtualMachine.objects.filter(region=region.region_id).exclude(vm_id='').count(),
                'profiles': VMProfile.objects.filter(office_network__region=region).count(),
            })
        context['enabled_rows'] = rows
        return context

    def form_valid(self, form):
        chosen = list(form.cleaned_data['regions'])
        before = set(CloudRegion.objects.filter(enabled=True).values_list('region_id', flat=True))
        CloudRegion.objects.exclude(pk__in=[region.pk for region in chosen]).update(enabled=False)
        failed = []
        for region in chosen:
            if not region.enabled:
                region.enabled = True
                region.save(update_fields=['enabled', 'modified'])
            try:
                sync_region(region)
            except AlibabaApiError as exc:
                failed.append('{}: {}'.format(region, exc))
        after = sorted(region.region_id for region in chosen)
        if set(after) != before:
            log_desktop(
                self.request, None, 'Update',
                old_value=', '.join(sorted(before)),
                new_value=', '.join(after),
                details='Changed the regions in use.',
                desktop_name='Regions',
            )
        if failed:
            messages.warning(self.request, 'Some regions could not be loaded from Alibaba. {}'.format(' '.join(failed)))
        else:
            messages.success(self.request, 'Regions saved. Office networks and desktop types are loaded from Alibaba.')
        return HttpResponseRedirect(reverse('view_dashboards:region_list'))


class RegionSyncView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def post(self, request, *args, **kwargs):
        failed = []
        try:
            sync_region_list()
        except AlibabaApiError as exc:
            failed.append('Region list: {}'.format(exc))
        for region in CloudRegion.objects.filter(enabled=True):
            try:
                sync_region(region)
            except AlibabaApiError as exc:
                failed.append('{}: {}'.format(region, exc))
        if failed:
            messages.warning(request, 'Some regions could not be loaded from Alibaba. {}'.format(' '.join(failed)))
        else:
            messages.success(request, 'Reloaded office networks and desktop types from Alibaba.')
        return HttpResponseRedirect(reverse('view_dashboards:region_list'))


class DesktopWebsiteView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = DesktopWebsiteForm
    cancel_url_name = 'view_dashboards:desktop_list'

    def dispatch(self, request, *args, **kwargs):
        self.desktop = get_object_or_404(VirtualMachine, pk=kwargs['pk'])
        self.current_access = {"mode": "off", "domains": [], "editable": False, "name": ""}
        self.shared_policy = True
        self._websites_loaded = False
        return super(DesktopWebsiteView, self).dispatch(request, *args, **kwargs)

    def _load_websites(self):
        if self._websites_loaded:
            return
        self._websites_loaded = True
        if not self.desktop.vm_id:
            return
        try:
            cloud = fetch_desktop(self.desktop)
            policy_id = desktop_policy_id_from(cloud)
            with in_region(self.desktop.region):
                policy = describe_policy(policy_id)
            self.current_access = website_access(policy)
            self.shared_policy = policy_is_shared(self.desktop, policy_id, policy)
        except AlibabaApiError as exc:
            messages.warning(self.request, "Could not load this computer's websites: {}".format(exc))

    def get_website_formset(self):
        initial = [{"domain": domain} for domain in display_websites(self.current_access["domains"])]
        if self.request.method == "POST":
            return WebsiteRuleFormSet(self.request.POST, prefix="websites")
        return WebsiteRuleFormSet(initial=initial, prefix="websites")

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        formset = self.get_website_formset()
        self.website_formset = formset
        if form.is_valid() and formset.is_valid() and self._apply_website_domains(form, formset):
            return self.form_valid(form)
        return self.render_to_response(self.get_context_data(form=form, website_formset=formset))

    def _apply_website_domains(self, form, formset):
        mode = form.cleaned_data.get("website_mode") or "off"
        domains = website_domains(formset)
        if domains is None:
            return False
        if mode == "off":
            form.cleaned_data["websites"] = []
            return True
        if not domains:
            form.add_error("website_mode", "Add at least one domain, or choose no restriction.")
            return False
        form.cleaned_data["websites"] = domains
        return True

    def get_form_kwargs(self):
        self._load_websites()
        kwargs = super(DesktopWebsiteView, self).get_form_kwargs()
        if not self.request.POST:
            kwargs["initial"] = {"website_mode": self.current_access["mode"]}
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(DesktopWebsiteView, self).get_context_data(**kwargs)
        if "website_formset" not in context:
            context["website_formset"] = getattr(self, "website_formset", None) or self.get_website_formset()
        context["page_title"] = "Websites for {}".format(self.desktop.name or "this computer")
        context["main_nav"] = "desktops"
        context["submit_label"] = "Save websites"
        context["cancel_url"] = reverse("view_dashboards:desktop_detail", args=[self.desktop.pk])
        if self.shared_policy:
            context["page_subtitle"] = (
                "This computer still uses the shared list. Saving copies it and applies the change only here. "
                "Other computers stay as they are."
            )
        else:
            context["page_subtitle"] = (
                "This computer already has its own list. Saving changes only this computer."
            )
        return context

    def form_valid(self, form):
        if not self.desktop.vm_id:
            form.add_error(None, "Save an Alibaba desktop ID before setting websites.")
            return self.form_invalid(form)
        mode = form.cleaned_data["website_mode"]
        domains = form.cleaned_data["websites"]
        if mode == self.current_access["mode"] and domains == expand_websites(self.current_access["domains"]):
            messages.info(self.request, "No change to this computer's websites.")
            return HttpResponseRedirect(reverse("view_dashboards:desktop_detail", args=[self.desktop.pk]))
        try:
            save_desktop_websites(self.desktop, mode, domains)
        except AlibabaApiError as exc:
            log_desktop(self.request, self.desktop, "API Error", details=str(exc))
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        log_desktop(
            self.request,
            self.desktop,
            "Update",
            old_value=self.current_access["mode"],
            new_value="{} {}".format(mode, ", ".join(domains)),
            details="Changed which websites this computer can open.",
        )
        messages.success(self.request, "Saved the website list for this computer only.")
        return HttpResponseRedirect(reverse("view_dashboards:desktop_detail", args=[self.desktop.pk]))


class DesktopSecureDnsView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def post(self, request, pk):
        vm = get_object_or_404(VirtualMachine, pk=pk)
        detail_url = reverse("view_dashboards:desktop_detail", args=[pk])
        if not vm.vm_id:
            messages.error(request, "Save an Alibaba desktop ID before locking Secure DNS.")
            return HttpResponseRedirect(detail_url)
        try:
            lock_browser_secure_dns(vm)
        except AlibabaApiError as exc:
            log_desktop(request, vm, "API Error", details=str(exc))
            messages.error(request, str(exc))
            return HttpResponseRedirect(detail_url)
        log_desktop(
            request,
            vm,
            "Update",
            details="Locked Chrome and Edge Secure DNS so website lists cannot be bypassed.",
        )
        messages.success(
            request,
            "Secure DNS is locked on this computer. Close Chrome and Edge once, then open them again.",
        )
        return HttpResponseRedirect(detail_url)


class RegionPolicyView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = RegionPolicyForm
    cancel_url_name = 'view_dashboards:region_list'

    def dispatch(self, request, *args, **kwargs):
        self.region = get_object_or_404(CloudRegion, pk=kwargs['pk'], enabled=True)
        return super(RegionPolicyView, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(RegionPolicyView, self).get_form_kwargs()
        self.current_access = {"mode": "off", "domains": [], "editable": False, "name": ""}
        try:
            with in_region(self.region.region_id):
                self.policies = describe_policy_groups()
                if self.region.policy_group_id:
                    self.current_access = website_access(describe_policy(self.region.policy_group_id))
        except AlibabaApiError as exc:
            self.policies = []
            messages.warning(self.request, 'Could not load the policies for {}: {}'.format(self.region, exc))
        if self.region.policy_group_id and self.region.policy_group_id not in dict(self.policies):
            self.policies.append((self.region.policy_group_id, self.region.policy_group_name or self.region.policy_group_id))
        kwargs['policies'] = self.policies
        if not self.request.POST:
            kwargs['initial'] = {
                'policy_group_id': self.region.policy_group_id,
                'website_mode': self.current_access['mode'],
            }
        return kwargs

    def get_website_formset(self):
        initial = [{"domain": domain} for domain in display_websites(self.current_access["domains"])]
        if self.request.method == "POST":
            return WebsiteRuleFormSet(self.request.POST, prefix="websites")
        return WebsiteRuleFormSet(initial=initial, prefix="websites")

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        formset = self.get_website_formset()
        self.website_formset = formset
        if form.is_valid() and formset.is_valid() and self._apply_website_domains(form, formset):
            return self.form_valid(form)
        return self.render_to_response(self.get_context_data(form=form, website_formset=formset))

    def _apply_website_domains(self, form, formset):
        mode = form.cleaned_data.get("website_mode") or "off"
        domains = website_domains(formset)
        if domains is None:
            return False
        if mode == "off":
            form.cleaned_data["websites"] = []
            return True
        if not domains:
            form.add_error("website_mode", "Add at least one domain, or choose no restriction.")
            return False
        form.cleaned_data["websites"] = domains
        return True

    def get_context_data(self, **kwargs):
        context = super(RegionPolicyView, self).get_context_data(**kwargs)
        if "website_formset" not in context:
            context["website_formset"] = getattr(self, "website_formset", None) or self.get_website_formset()
        context['page_title'] = 'Policy for {}'.format(self.region)
        context['main_nav'] = 'regions'
        context['submit_label'] = 'Save policy'
        context['page_subtitle'] = (
            'The recording policy applies to new computers in {}. '
            'The website list is the shared list. A computer with its own list, set on that computer, is not changed.'
        ).format(self.region)
        return context

    def form_valid(self, form):
        policy_id = form.cleaned_data['policy_group_id']
        mode = form.cleaned_data['website_mode']
        domains = form.cleaned_data['websites']
        websites_changed = (
            mode != self.current_access['mode'] or domains != expand_websites(self.current_access['domains'])
        )
        if websites_changed:
            if not policy_id:
                form.add_error(
                    'website_mode',
                    'Choose a custom policy before setting websites. Alibaba\'s default policy cannot be changed.',
                )
                return self.form_invalid(form)
            try:
                with in_region(self.region.region_id):
                    set_website_access(policy_id, mode, domains)
            except AlibabaApiError as exc:
                log_desktop(
                    self.request, None, 'API Error', details=str(exc),
                    desktop_name='Region {}'.format(self.region), region=self.region.region_id,
                )
                form.add_error(None, str(exc))
                return self.form_invalid(form)
            log_desktop(
                self.request, None, 'Update',
                old_value=self.current_access['mode'],
                new_value='{} {}'.format(mode, ', '.join(domains)),
                details='Changed which websites computers can open.',
                desktop_name='Region {}'.format(self.region),
                region=self.region.region_id,
            )
        old_value = self.region.policy_group_name or self.region.policy_group_id or 'Alibaba default policy'
        return self._save_policy_choice(form, policy_id, websites_changed, old_value)

    def _save_policy_choice(self, form, policy_id, websites_changed, old_value):
        stored_id = CloudRegion.objects.filter(pk=self.region.pk).values_list('policy_group_id', flat=True).first() or ''
        self.region.policy_group_id = policy_id
        self.region.policy_group_name = dict(self.policies).get(policy_id, '') if policy_id else ''
        self.region.save(update_fields=['policy_group_id', 'policy_group_name', 'modified'])
        if stored_id != (policy_id or ''):
            log_desktop(
                self.request, None, 'Update',
                old_value=old_value,
                new_value=self.region.policy_group_name or policy_id or 'Alibaba default policy',
                details='Changed the recording policy for new computers.',
                desktop_name='Region {}'.format(self.region),
                region=self.region.region_id,
            )
        if websites_changed:
            messages.success(
                self.request,
                'Website access saved. Every Windows computer on this policy uses it now.',
            )
        else:
            messages.success(self.request, 'Policy saved for {}.'.format(self.region))
        return HttpResponseRedirect(reverse('view_dashboards:region_list'))


class DesktopBandwidthRedirectView(RedirectView):
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        vm = VirtualMachine.objects.filter(pk=kwargs.get("pk")).first()
        network = OfficeNetwork.objects.filter(directory_id=vm.directory_id).first() if vm else None
        if network is not None:
            return reverse("view_dashboards:office_network_detail", args=[network.pk])
        return reverse("view_dashboards:office_network")


class DesktopSpecView(GroupRequiredMixin, DashboardFormMixin, FormView):
    group_required = ['superadmin']
    form_class = DesktopSpecForm
    cancel_url_name = 'view_dashboards:desktop_list'

    def dispatch(self, request, *args, **kwargs):
        self.desktop = get_object_or_404(VirtualMachine, pk=kwargs['pk'])
        if self.desktop.vm_id:
            try:
                refresh_desktop(self.desktop)
            except AlibabaApiError:
                pass
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
        updating = self.desktop.status in ('Upgrading', 'Updating')
        if updating:
            context['form_locked'] = True
            context['page_subtitle'] = (
                'This computer is updating. Save stays off until Alibaba finishes, so a second change does not fail.'
            )
            context['update_progress'] = {
                'title': 'Updating specifications',
                'label': self.desktop.get_status_display(),
                'detail': 'Wait until the bar finishes before changing this computer again.',
                'poll_url': reverse('view_dashboards:desktop_progress'),
                'poll_mode': 'desktop',
                'poll_id': self.desktop.pk,
            }
        elif self.desktop.status != 'Stopped':
            context['page_subtitle'] = 'This computer is {}. Stop it before changing specifications. {}'.format(
                self.desktop.get_status_display(), rules,
            )
        else:
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
                desktop.region or None,
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
            office_network__directory_id=desktop.directory_id,
        ).first()
        desktop.status = 'Upgrading'
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
            'Alibaba is upgrading this computer. Files and Windows stay. The status shows Upgrading until it finishes, then you can start it.',
        )
        return HttpResponseRedirect(reverse('view_dashboards:desktop_detail', args=[desktop.pk]))


class VMProfileListView(GroupRequiredMixin, DashboardMixin, ListView):
    group_required = ['superadmin']
    model = VMProfile
    template_name = 'view_dashboards/_group_dashboard/superadmin/vm_profile_list.html'
    context_object_name = 'vm_profiles'
    paginate_by = 20

    def get_queryset(self):
        return VMProfile.objects.select_related('office_network__region').order_by('name')

    def get_context_data(self, **kwargs):
        context = super(VMProfileListView, self).get_context_data(**kwargs)
        context['page_title'] = 'VM Profiles'
        context['main_nav'] = 'vm_profiles'
        return context


def network_type_map():
    return {
        str(network.pk): network.desktop_types or []
        for network in OfficeNetwork.objects.filter(region__enabled=True)
    }


class VMProfileCreateView(GroupRequiredMixin, DashboardFormMixin, CreateView):
    group_required = ['superadmin']
    model = VMProfile
    form_class = VMProfileForm
    page_subtitle = (
        'Hardware template for one office network. The desktop types listed are the ones Alibaba sells there. '
        'Internet is set on Network.'
    )
    cancel_url_name = 'view_dashboards:vm_profile_list'

    def get_context_data(self, **kwargs):
        context = super(VMProfileCreateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Add VM profile'
        context['main_nav'] = 'vm_profiles'
        context['network_types'] = network_type_map()
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.created_by = self.request.user
        profile.save()
        self.object = profile
        log_desktop(
            self.request, None, "Create",
            new_value=spec_summary(profile.instance_type_code, profile.system_disk, profile.data_disk),
            details="Saved a profile.",
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
    page_subtitle = (
        'Update this template. The desktop types listed are the ones Alibaba sells in the chosen office network. '
        'Internet is set on Network.'
    )
    cancel_url_name = 'view_dashboards:vm_profile_list'

    def get_context_data(self, **kwargs):
        context = super(VMProfileUpdateView, self).get_context_data(**kwargs)
        context['page_title'] = 'Edit VM profile'
        context['main_nav'] = 'vm_profiles'
        context['network_types'] = network_type_map()
        return context

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.modified_by = self.request.user
        profile.save()
        self.object = profile
        log_desktop(
            self.request, None, "Update",
            new_value=spec_summary(profile.instance_type_code, profile.system_disk, profile.data_disk),
            details="Updated a profile.",
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

class SignInHistoryView(GroupRequiredMixin, DashboardMixin, TemplateView):
    group_required = ['superadmin']
    template_name = "view_dashboards/_group_dashboard/superadmin/signin_history.html"

    def get_context_data(self, **kwargs):
        context = super(SignInHistoryView, self).get_context_data(**kwargs)
        context["page_title"] = "Sign-in"
        context["main_nav"] = "signin"
        days = self.request.GET.get("days", "7")
        if days not in ("7", "30"):
            days = "7"
        selected_user = (self.request.GET.get("user") or "").strip()
        profiles = list(
            Profile.objects.exclude(eds_end_user_id__isnull=True)
            .exclude(eds_end_user_id="")
            .select_related("user")
            .order_by("eds_end_user_id")
        )
        known_ids = {profile.eds_end_user_id for profile in profiles}
        if selected_user and selected_user not in known_ids:
            selected_user = ""
        context["days"] = days
        context["selected_user"] = selected_user
        context["wuying_users"] = profiles
        context["signin_error"] = ""
        context["signin_events"] = []

        end = timezone.now()
        start = end - timedelta(days=int(days))
        try:
            raw_events = describe_signin_events(start, end, selected_user)
        except AlibabaApiError as exc:
            context["signin_error"] = str(exc)
            return context

        names = {profile.eds_end_user_id: profile.user.username for profile in profiles}
        rows = []
        for event in raw_events:
            event_type = event.get("EventType") or ""
            if event_type not in SIGNIN_EVENT_LABELS:
                continue
            rows.append({
                "when": parse_alibaba_time(event.get("EventTime")),
                "label": SIGNIN_EVENT_LABELS[event_type],
                "end_user_id": event.get("EndUserId") or "",
                "username": names.get(event.get("EndUserId") or "", ""),
                "desktop_name": event.get("DesktopName") or "",
                "desktop_id": event.get("DesktopId") or "",
                "client_ip": event.get("ClientIp") or "",
            })
        rows.sort(key=lambda row: row["when"] or start, reverse=True)
        context["signin_events"] = rows
        return context


class RecordingListView(GroupRequiredMixin, DashboardMixin, TemplateView):
    group_required = ['superadmin']
    template_name = "view_dashboards/_group_dashboard/superadmin/recording_list.html"

    def get_context_data(self, **kwargs):
        context = super(RecordingListView, self).get_context_data(**kwargs)
        context["page_title"] = "Recordings"
        context["main_nav"] = "recordings"
        days = self.request.GET.get("days", "7")
        if days not in ("7", "30"):
            days = "7"
        selected_range, range_error = parse_date_range(
            self.request.GET.get("from"),
            self.request.GET.get("to"),
        )
        selected_user = (self.request.GET.get("user") or "").strip()
        selected_desktop = (self.request.GET.get("desktop") or "").strip()
        report_user = (self.request.GET.get("report_user") or "").strip()
        profiles = list(
            Profile.objects.exclude(eds_end_user_id__isnull=True)
            .exclude(eds_end_user_id="")
            .select_related("user")
            .order_by("eds_end_user_id")
        )
        desktops = list(VirtualMachine.objects.exclude(vm_id__isnull=True).exclude(vm_id="").order_by("name"))
        known_ids = {profile.eds_end_user_id for profile in profiles}
        known_desktops = {desktop.vm_id for desktop in desktops}
        if selected_user and selected_user not in known_ids:
            selected_user = ""
        if report_user and report_user not in known_ids:
            report_user = ""
        if selected_desktop and selected_desktop not in known_desktops:
            selected_desktop = ""
        context["days"] = days
        context["date_from"] = selected_range[2] if selected_range else (self.request.GET.get("from") or "")
        context["date_to"] = selected_range[3] if selected_range else (self.request.GET.get("to") or "")
        context["range_start"] = selected_range[0] if selected_range else None
        context["range_end"] = selected_range[1] if selected_range else None
        context["range_ready"] = selected_range is not None
        context["range_error"] = range_error
        context["selected_user"] = selected_user
        context["selected_desktop"] = selected_desktop
        context["report_user"] = report_user
        context["wuying_users"] = profiles
        context["desktops"] = desktops
        context["recording_error"] = ""
        context["recordings"] = []
        context["work_notes"] = []
        context["summaries_waiting"] = 0
        context["report_error"] = ""

        end = timezone.now()
        start = end - timedelta(days=int(days))
        try:
            raw_recordings = describe_recordings(start, end, selected_desktop)
        except AlibabaApiError as exc:
            context["recording_error"] = str(exc)
            raw_recordings = []

        names = {profile.eds_end_user_id: profile.user.username for profile in profiles}
        desktop_links = {desktop.vm_id: desktop for desktop in desktops}
        rows = []
        for item in raw_recordings:
            end_user_ids = item.get("EndUserIds") or []
            if isinstance(end_user_ids, str):
                end_user_ids = [end_user_ids]
            end_user_id = end_user_ids[0] if end_user_ids else ""
            if selected_user and selected_user not in end_user_ids:
                continue
            started = parse_alibaba_time(item.get("StartTime"))
            if started is not None and (started < start or started > end):
                continue
            finished = parse_alibaba_time(item.get("EndTime"))
            size = int(item.get("RecordingSize") or 0)
            if size >= 1024 * 1024:
                size_label = "{:.1f} MB".format(size / (1024 * 1024))
            elif size >= 1024:
                size_label = "{:.0f} KB".format(size / 1024)
            else:
                size_label = "{} B".format(size)
            desktop_id = item.get("DesktopId") or ""
            rows.append({
                "started": started,
                "finished": finished,
                "end_user_id": end_user_id,
                "username": names.get(end_user_id, ""),
                "desktop_name": item.get("DesktopName") or "",
                "desktop_id": desktop_id,
                "desktop": desktop_links.get(desktop_id),
                "size_label": size_label,
                "file_path": item.get("FilePath") or "",
            })
        rows.sort(key=lambda row: row["started"] or start, reverse=True)
        context["recordings"] = rows
        if selected_range is not None:
            try:
                report_rows = recording_rows(selected_range[0], selected_range[1], report_user)
            except AlibabaApiError as exc:
                context["report_error"] = str(exc)
            else:
                context["work_notes"] = work_notes(report_rows)
                context["summaries_waiting"] = pending_count(report_rows)
        return context


class RecordingSummarizeView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def post(self, request, *args, **kwargs):
        selected_range, range_error = parse_date_range(request.POST.get("from"), request.POST.get("to"))
        if selected_range is None:
            return JsonResponse({"ok": False, "error": range_error or "Choose a start date and an end date first."}, status=400)
        report_user = (request.POST.get("report_user") or "").strip()
        payload = summarize_next(selected_range[0], selected_range[1], report_user, "")
        status = 200 if payload.get("ok") else 502
        return JsonResponse(payload, status=status)


RECORDING_PATH = re.compile(r"^[A-Za-z0-9_./+-]+\.mp4$")


class RecordingMediaView(GroupRequiredMixin, DashboardMixin, View):
    group_required = ['superadmin']

    def get(self, request, *args, **kwargs):
        file_path = (request.GET.get("path") or "").strip()
        if not RECORDING_PATH.match(file_path) or ".." in file_path:
            raise Http404
        signed_url = recording_signed_url(file_path)
        parsed = urlparse(signed_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host.endswith(".aliyuncs.com") or host.endswith(".alibabacloud.com")
        ):
            raise Http404

        headers = {"User-Agent": "machine-dashboard"}
        range_header = request.META.get("HTTP_RANGE")
        if range_header:
            headers["Range"] = range_header
        try:
            upstream = urlopen(Request(signed_url, headers=headers), timeout=60)
        except (HTTPError, URLError):
            return HttpResponse(
                "The recording could not be opened. Refresh Recordings and try again.",
                status=502,
                content_type="text/plain",
            )

        filename = file_path.rsplit("/", 1)[-1].replace('"', "")
        disposition = "attachment" if request.GET.get("download") == "1" else "inline"

        def chunks():
            try:
                while True:
                    block = upstream.read(64 * 1024)
                    if not block:
                        break
                    yield block
            finally:
                upstream.close()

        response = StreamingHttpResponse(chunks(), status=upstream.status, content_type="video/mp4")
        response["Content-Disposition"] = '{}; filename="{}"'.format(disposition, filename)
        response["Accept-Ranges"] = "bytes"
        response["Cache-Control"] = "private, no-store"
        content_length = upstream.headers.get("Content-Length")
        content_range = upstream.headers.get("Content-Range")
        if content_length:
            response["Content-Length"] = content_length
        if content_range:
            response["Content-Range"] = content_range
        return response


class GuidanceView(GroupRequiredMixin, DashboardMixin, TemplateView):
    group_required = ['superadmin']
    template_name = "view_dashboards/_group_dashboard/superadmin/guidance.html"

    def get_context_data(self, **kwargs):
        context = super(GuidanceView, self).get_context_data(**kwargs)
        context["page_title"] = "Guidance"
        context["main_nav"] = "guidance"
        context["wuying_download_url"] = WUYING_DOWNLOAD_URL
        context["wuying_web_url"] = WUYING_WEB_URL
        context["office_networks"] = enabled_networks()
        context["web_sdk_ready"] = web_sdk_installed()
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
                "detail": vm.status_detail,
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
                delete_cloud_desktop(vm.vm_id, vm.region or None)
                mark_desktop_released(vm.vm_id)
            except AlibabaApiError as exc:
                code = (exc.code or "").lower()
                message = str(exc).lower()
                missing = "notfound" in code or "not found" in message or "not exist" in message
                if not missing:
                    vm.status_detail = str(exc)[:255]
                    vm.save(update_fields=["status_detail", "modified"])
                    log_desktop(self.request, vm, "API Error", details=str(exc))
                    messages.error(self.request, str(exc))
                    return HttpResponseRedirect(reverse("view_dashboards:desktop_detail", args=[vm.pk]))
                mark_desktop_released(vm.vm_id)
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
            refresh_desktop(vm)
        except AlibabaApiError:
            pass
        if action == "stop" and vm.status == "Stopped":
            messages.info(request, "This computer is already stopped.")
            return HttpResponseRedirect(detail_url)
        if action == "start" and vm.status == "Running":
            messages.info(request, "This computer is already running.")
            return HttpResponseRedirect(detail_url)
        if vm.status in ("Upgrading", "Updating"):
            messages.info(
                request,
                vm.status_detail or "This computer is still updating. Wait until that finishes, then try again.",
            )
            return HttpResponseRedirect(detail_url)
        try:
            operate_desktop(request, vm, *spec)
        except AlibabaApiError as exc:
            if action == "stop" and "not running" in str(exc).lower():
                vm.status = "Stopped"
                vm.save(update_fields=["status", "modified"])
                messages.info(request, "Alibaba already has this computer stopped. The status is updated.")
            else:
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


WORKER_STATUS_LABELS = {
    "Running": "Ready",
    "Stopped": "Turned off",
    "Starting": "Turning on",
    "Stopping": "Turning off",
    "Creating": "Being prepared",
    "Upgrading": "Being updated",
    "Updating": "Being updated",
}
WEB_SDK_SCRIPT = "wuying-sdk/WuyingWebSDK.js"
WEB_SDK_CONTAINER = "wuying-sdk/sdk/ASP/container.html"


def web_sdk_installed():
    from django.contrib.staticfiles import finders
    return bool(finders.find(WEB_SDK_SCRIPT)) and bool(finders.find(WEB_SDK_CONTAINER))


def worker_desktops(user):
    return VirtualMachine.objects.filter(user=user).exclude(vm_id="").order_by("name")


class MyComputerView(WorkerRequiredMixin, TemplateView):
    template_name = "view_dashboards/_group_dashboard/worker/my_computer.html"

    def get_context_data(self, **kwargs):
        context = super(MyComputerView, self).get_context_data(**kwargs)
        rows = []
        for vm in worker_desktops(self.request.user):
            try:
                vm = refresh_desktop(vm) or vm
            except AlibabaApiError:
                pass
            rows.append({
                "vm": vm,
                "label": WORKER_STATUS_LABELS.get(vm.status, "Not available"),
                "busy": vm.status in BUSY_STATUSES,
            })
        context['page_title'] = 'My computer'
        context['main_nav'] = 'my_computer'
        context['computers'] = rows
        context['any_busy'] = any(row["busy"] for row in rows)
        return context


class MyComputerStartView(WorkerRequiredMixin, View):
    def post(self, request, pk):
        vm = get_object_or_404(worker_desktops(request.user), pk=pk)
        back = reverse("view_dashboards:my_computer")
        try:
            refresh_desktop(vm)
        except AlibabaApiError:
            pass
        if vm.status == "Running":
            messages.info(request, "Your computer is already on.")
            return HttpResponseRedirect(back)
        if vm.status in ("Upgrading", "Updating"):
            messages.info(request, "Your computer is being updated. Wait until that finishes.")
            return HttpResponseRedirect(back)
        if vm.status != "Stopped":
            messages.info(request, "Your computer is busy. Wait a moment and try again.")
            return HttpResponseRedirect(back)
        try:
            operate_desktop(request, vm, *POWER_ACTIONS["start"])
        except AlibabaApiError:
            messages.error(request, "Your computer could not be turned on. Ask your administrator.")
        else:
            messages.success(request, "Turning on your computer. This takes about a minute.")
        return HttpResponseRedirect(back)


class MyComputerConnectView(WorkerRequiredMixin, TemplateView):
    template_name = "view_dashboards/_group_dashboard/worker/connect.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.groups.exists():
            self.vm = get_object_or_404(worker_desktops(request.user), pk=kwargs['pk'])
        return super(MyComputerConnectView, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        try:
            refresh_desktop(self.vm)
        except AlibabaApiError:
            pass
        if self.vm.status != "Running":
            messages.info(request, "Turn on your computer first.")
            return HttpResponseRedirect(reverse("view_dashboards:my_computer"))
        return super(MyComputerConnectView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(MyComputerConnectView, self).get_context_data(**kwargs)
        context['page_title'] = self.vm.name or 'My computer'
        context['computer'] = self.vm
        context['sdk_ready'] = web_sdk_installed()
        context['sdk_script'] = WEB_SDK_SCRIPT
        context['sdk_container'] = WEB_SDK_CONTAINER
        context['session_config'] = {
            'desktopId': self.vm.vm_id,
            'regionId': self.vm.region,
            'overseas': not (self.vm.region or '').startswith('cn-'),
            'codeUrl': reverse('view_dashboards:my_computer_code', args=[self.vm.pk]),
            'backUrl': reverse('view_dashboards:my_computer'),
        }
        return context


class MyComputerCodeView(WorkerRequiredMixin, View):
    def post(self, request, pk):
        vm = get_object_or_404(worker_desktops(request.user), pk=pk)
        if vm.status != "Running":
            return JsonResponse({"detail": "Turn on your computer first."}, status=409)
        try:
            ticket = desktop_auth_code(vm)
        except AlibabaApiError as exc:
            write_log(request, vm, "API Error", str(exc))
            return JsonResponse({"detail": "Your computer could not be opened. Ask your administrator."}, status=502)
        write_log(request, vm, "Connect", "Opened in the browser")
        response = JsonResponse({"ticket": ticket})
        response["Cache-Control"] = "no-store"
        return response