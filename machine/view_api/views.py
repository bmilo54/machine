import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.profiles.models import Profile
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine

ACCESS_KEY_ID = settings.ACCESS_KEY_ID
ACCESS_KEY_SECRET = settings.ACCESS_KEY_SECRET
REGION_ID = settings.REGION_ID
DIRECTORY_ID = settings.DIRECTORY_ID

ECD_VERSION = "2020-09-30"
WINDOWS_11_PRO_IMAGE_ID = "desktopimage-windows-11-64-asp"
DEFAULT_DESKTOP_TYPE = "eds.enterprise_office.6c12g"
DEFAULT_POLICY_GROUP_ID = "system-all-enabled-policy"
STATUS_FROM_CLOUD = {
    "Running": "Running",
    "Stopped": "Stopped",
    "Starting": "Starting",
    "Stopping": "Stopping",
    "Pending": "Creating",
    "Rebuilding": "Creating",
    "Expired": "Error",
    "Deleted": "Error",
}


class AlibabaApiError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def login_required_json(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Login required."}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def staff_required_json(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Login required."}, status=401)
        if not (request.user.is_staff or request.user.is_superuser):
            return JsonResponse({"detail": "Admin only."}, status=403)
        return view(request, *args, **kwargs)

    return wrapper


def percent_encode(value):
    encoded = quote(str(value), safe="")
    return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def call_ecd(action, extra=None):
    params = {
        "Action": action,
        "Format": "JSON",
        "Version": ECD_VERSION,
        "AccessKeyId": ACCESS_KEY_ID,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "RegionId": REGION_ID,
    }
    if extra:
        params.update(extra)

    canonical = "&".join(
        f"{percent_encode(key)}={percent_encode(params[key])}" for key in sorted(params)
    )
    string_to_sign = f"GET&%2F&{percent_encode(canonical)}"
    digest = hmac.new(
        f"{ACCESS_KEY_SECRET}&".encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    params["Signature"] = base64.b64encode(digest).decode("utf-8")
    query = "&".join(
        f"{percent_encode(key)}={percent_encode(params[key])}" for key in sorted(params)
    )
    request = Request(f"https://ecd.{REGION_ID}.aliyuncs.com/?{query}")

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"Message": raw or exc.reason}
        raise AlibabaApiError(
            payload.get("Message") or "Alibaba Cloud request failed.",
            payload.get("Code") or str(exc.code),
        ) from exc
    except URLError as exc:
        raise AlibabaApiError(f"Could not reach Alibaba Cloud: {exc.reason}") from exc

    if payload.get("Code"):
        raise AlibabaApiError(payload.get("Message") or "Alibaba Cloud request failed.", payload["Code"])
    return payload


def desktop_type_resources(desktop_type):
    matched = None
    for part in str(desktop_type or "").split("."):
        if "c" in part and part.endswith("g"):
            matched = part
    if not matched or "c" not in matched:
        return None, None
    cpu_text, ram_text = matched[:-1].split("c", 1)
    if cpu_text.isdigit() and ram_text.isdigit():
        return int(cpu_text), int(ram_text)
    return None, None


def resolve_desktop_spec(profile):
    desktop_type = DEFAULT_DESKTOP_TYPE
    if profile and profile.instance_type_code:
        desktop_type = profile.instance_type_code.strip() or DEFAULT_DESKTOP_TYPE
    elif profile and profile.cpu == 8 and profile.ram == 16:
        desktop_type = "eds.enterprise_office.8c16g"

    cpu, ram = desktop_type_resources(desktop_type)
    if profile and cpu is None:
        cpu = profile.cpu
    if profile and ram is None:
        ram = profile.ram

    system_disk = 80
    data_disk = 0
    if profile and profile.system_disk and profile.system_disk >= 80:
        system_disk = profile.system_disk
    if profile and profile.data_disk and profile.data_disk >= 40:
        data_disk = profile.data_disk
    return desktop_type, cpu, ram, system_disk, data_disk


def desktop_id_from(payload):
    desktop_ids = payload.get("DesktopId") or []
    if isinstance(desktop_ids, str):
        desktop_ids = [desktop_ids]
    if not desktop_ids:
        raise AlibabaApiError("Alibaba did not return a desktop ID.")
    desktop_id = desktop_ids[0]
    if isinstance(desktop_id, (list, tuple)):
        desktop_id = desktop_id[0]
    return str(desktop_id).strip().strip('[]"')


def create_cloud_desktop(name, profile=None, end_user_id=""):
    if not DIRECTORY_ID or DIRECTORY_ID == "xxxx":
        raise AlibabaApiError("Set DIRECTORY_ID in local settings to the Hangzhou office network.")

    desktop_type, cpu, ram, system_disk, data_disk = resolve_desktop_spec(profile)
    extra = {
        "DesktopName": name,
        "Amount": 1,
        "OfficeSiteId": DIRECTORY_ID,
        "PolicyGroupId": DEFAULT_POLICY_GROUP_ID,
        "ChargeType": "PostPaid",
        "DesktopAttachment.ImageId": WINDOWS_11_PRO_IMAGE_ID,
        "DesktopAttachment.SystemDiskSize": system_disk,
        "DesktopAttachment.DataDiskSize": data_disk,
        "DesktopAttachment.DesktopType": desktop_type,
    }
    if end_user_id:
        extra["EndUserId.1"] = end_user_id

    payload = call_ecd("CreateDesktops", extra)
    return {
        "vm_id": desktop_id_from(payload),
        "cpu": cpu,
        "ram": ram,
        "system_disk": system_disk,
        "data_disk": data_disk,
    }


def delete_cloud_desktop(vm_id):
    if not vm_id:
        return
    call_ecd("DeleteDesktops", {"DesktopId.1": vm_id})


def describe_desktops():
    desktops = []
    next_token = ""
    for _ in range(20):
        extra = {"MaxResults": 100}
        if DIRECTORY_ID and DIRECTORY_ID != "xxxx":
            extra["DirectoryId"] = DIRECTORY_ID
        if next_token:
            extra["NextToken"] = next_token
        payload = call_ecd("DescribeDesktops", extra)
        desktops.extend(payload.get("Desktops") or [])
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
    return desktops


def memory_gb(value):
    if value in (None, ""):
        return None
    mib = int(value)
    if mib >= 512:
        return max(1, round(mib / 1024))
    return mib


def disk_gb(desktop, disk_type):
    for disk in desktop.get("Disks") or []:
        if disk.get("DiskType") == disk_type and disk.get("DiskSize") not in (None, ""):
            return int(disk["DiskSize"])
    return None


def apply_desktop(desktop):
    vm_id = desktop.get("DesktopId")
    if not vm_id:
        return None

    vm, _created = VirtualMachine.objects.get_or_create(
        vm_id=vm_id,
        defaults={"region": REGION_ID, "status": "Creating"},
    )
    vm.name = desktop.get("DesktopName") or vm.name
    vm.region = REGION_ID
    vm.directory_id = desktop.get("DirectoryId") or desktop.get("OfficeSiteId") or vm.directory_id
    vm.status = STATUS_FROM_CLOUD.get(desktop.get("DesktopStatus"), "Error")
    vm.cpu = desktop.get("Cpu") if desktop.get("Cpu") is not None else vm.cpu
    ram = memory_gb(desktop.get("Memory"))
    if ram is not None:
        vm.ram = ram

    system_disk = disk_gb(desktop, "SYSTEM")
    data_disk = disk_gb(desktop, "DATA")
    if system_disk is not None:
        vm.system_disk = system_disk
    if data_disk is not None:
        vm.data_disk = data_disk

    private_ip = desktop.get("NetworkInterfaceIp")
    if private_ip:
        vm.private_ip = private_ip

    desktop_type = desktop.get("DesktopType")
    if desktop_type:
        profile = VMProfile.objects.filter(instance_type_code=desktop_type).first()
        if profile:
            vm.current_profile = profile

    end_user_ids = desktop.get("EndUserIds") or []
    if isinstance(end_user_ids, str):
        end_user_ids = [end_user_ids]
    profile = Profile.objects.filter(eds_end_user_id__in=end_user_ids).select_related("user").first()
    if profile:
        vm.user = profile.user

    vm.save()
    return vm


def desktop_payload(vm):
    return {
        "id": vm.id,
        "name": vm.name,
        "vm_id": vm.vm_id,
        "status": vm.status,
        "region": vm.region,
        "directory_id": vm.directory_id,
        "cpu": vm.cpu,
        "ram": vm.ram,
        "system_disk": vm.system_disk,
        "data_disk": vm.data_disk,
        "private_ip": vm.private_ip,
        "public_ip": vm.public_ip,
        "username": vm.user.username if vm.user_id else None,
    }


def visible_desktops(user):
    queryset = VirtualMachine.objects.select_related("user")
    if user.is_staff or user.is_superuser:
        return queryset
    return queryset.filter(user=user)


def can_operate(user, vm):
    return user.is_staff or user.is_superuser or vm.user_id == user.id


def client_ip(request):
    return request.META.get("REMOTE_ADDR") or None


def write_log(request, vm, action, details=""):
    ActivityLog.objects.create(
        actor=request.user,
        virtual_machine=vm,
        action=action,
        details=details,
        ip_address=client_ip(request),
    )


def operate_desktop(request, vm, action, ecd_action, next_status, time_field):
    if not vm.vm_id:
        raise AlibabaApiError("This desktop has no Alibaba desktop ID yet.")
    try:
        call_ecd(ecd_action, {"DesktopId.1": vm.vm_id})
    except AlibabaApiError as exc:
        write_log(request, vm, "API Error", f"{exc.code}: {exc}")
        raise
    vm.status = next_status
    if time_field == "last_start_time":
        vm.last_start_time = datetime.now(timezone.utc)
    elif time_field == "last_stop_time":
        vm.last_stop_time = datetime.now(timezone.utc)
    vm.save(update_fields=["status", time_field, "modified"])
    write_log(request, vm, action)
    return vm


def connection_ticket(vm):
    if not vm.vm_id:
        raise AlibabaApiError("This desktop has no Alibaba desktop ID yet.")
    if vm.status != "Running":
        raise AlibabaApiError("The desktop must be Running before a connection ticket can be issued.")

    extra = {"DesktopId": vm.vm_id}
    end_user_id = ""
    if vm.user_id:
        profile = Profile.objects.filter(user_id=vm.user_id).first()
        if profile and profile.eds_end_user_id:
            end_user_id = profile.eds_end_user_id
    if end_user_id:
        extra["EndUserId"] = end_user_id

    task_id = ""
    for _ in range(8):
        params = dict(extra)
        if task_id:
            params["TaskId"] = task_id
        payload = call_ecd("GetConnectionTicket", params)
        status = payload.get("TaskStatus")
        ticket = payload.get("Ticket")
        if status == "FINISHED" and ticket:
            return ticket
        if status == "FAILED":
            raise AlibabaApiError(payload.get("TaskMessage") or "Failed to get a connection ticket.")
        task_id = payload.get("TaskId") or task_id
        time.sleep(1)
    raise AlibabaApiError("The connection ticket is still being prepared. Try again in a moment.")


def run_desktop_action(request, pk, action, ecd_action, next_status, time_field):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not can_operate(request.user, vm):
        return JsonResponse({"detail": "You cannot operate this desktop."}, status=403)
    try:
        operate_desktop(request, vm, action, ecd_action, next_status, time_field)
    except AlibabaApiError as exc:
        return JsonResponse({"detail": str(exc), "code": exc.code}, status=502)
    return JsonResponse(desktop_payload(vm))


@require_GET
@login_required_json
def desktop_list(request):
    desktops = [desktop_payload(vm) for vm in visible_desktops(request.user)]
    return JsonResponse({"desktops": desktops})


@require_POST
@staff_required_json
def desktop_sync(request):
    try:
        desktops = describe_desktops()
    except AlibabaApiError as exc:
        return JsonResponse({"detail": str(exc), "code": exc.code}, status=502)

    saved = []
    for desktop in desktops:
        vm = apply_desktop(desktop)
        if vm:
            saved.append(desktop_payload(vm))
    return JsonResponse({"synced": len(saved), "desktops": saved})


@require_POST
@login_required_json
def desktop_start(request, pk):
    return run_desktop_action(request, pk, "Start", "StartDesktops", "Starting", "last_start_time")


@require_POST
@login_required_json
def desktop_stop(request, pk):
    return run_desktop_action(request, pk, "Stop", "StopDesktops", "Stopping", "last_stop_time")


@require_POST
@login_required_json
def desktop_restart(request, pk):
    return run_desktop_action(request, pk, "Restart", "RebootDesktops", "Starting", "last_start_time")
