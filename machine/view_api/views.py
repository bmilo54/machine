from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.profiles.models import Profile
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine

ACCESS_KEY_ID = settings.ACCESS_KEY_ID
ACCESS_KEY_SECRET = settings.ACCESS_KEY_SECRET
REGION_ID = settings.REGION_ID
DIRECTORY_ID = settings.DIRECTORY_ID

COMPUTE_SPECS = {
    "VALUE": (1, 2),
    "STANDARD": (2, 4),
    "PERFORMANCE": (2, 8),
    "POWER": (4, 16),
    "POWERPRO": (8, 32),
    "GENERALPURPOSE_4XLARGE": (16, 64),
    "GENERALPURPOSE_8XLARGE": (32, 128),
}
STATUS_FROM_CLOUD = {
    "AVAILABLE": "Running",
    "STOPPED": "Stopped",
    "SUSPENDED": "Stopped",
    "STARTING": "Starting",
    "REBOOTING": "Starting",
    "STOPPING": "Stopping",
    "PENDING": "Creating",
    "REBUILDING": "Creating",
    "RESTORING": "Creating",
    "UPDATING": "Creating",
    "TERMINATING": "Stopping",
    "TERMINATED": "Error",
    "ERROR": "Error",
    "UNHEALTHY": "Error",
    "IMPAIRED": "Error",
}


class CloudApiError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


AlibabaApiError = CloudApiError


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


def workspaces_client():
    if not ACCESS_KEY_ID or ACCESS_KEY_ID == "xxxx" or not ACCESS_KEY_SECRET or ACCESS_KEY_SECRET == "xxxx":
        raise CloudApiError("Set ACCESS_KEY_ID and ACCESS_KEY_SECRET in local settings to an AWS IAM access key.")
    if not REGION_ID or REGION_ID == "xxxx":
        raise CloudApiError("Set REGION_ID in local settings. Singapore is ap-southeast-1.")
    return boto3.client(
        "workspaces",
        region_name=REGION_ID,
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=ACCESS_KEY_SECRET,
    )


def aws_error(exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return CloudApiError(error.get("Message") or "AWS request failed.", error.get("Code") or "")
    return CloudApiError(str(exc))


def call_workspaces(method_name, **kwargs):
    client = workspaces_client()
    try:
        return client, getattr(client, method_name)(**kwargs)
    except (ClientError, BotoCoreError) as exc:
        raise aws_error(exc) from exc


def failed_request(payload):
    failed = payload.get("FailedRequests") or []
    if not failed:
        return None
    first = failed[0]
    return CloudApiError(
        first.get("ErrorMessage") or "AWS rejected the WorkSpaces request.",
        first.get("ErrorCode") or "",
    )


def bundle_score(bundle):
    name = (bundle.get("Name") or "").lower()
    if "windows" not in name:
        return 0
    if "server 2022" in name and "standard" in name:
        return 100
    if "standard" in name:
        return 80
    return 40


def choose_bundle(client, profile=None):
    configured = ""
    if profile and (profile.instance_type_code or "").startswith("wsb-"):
        configured = profile.instance_type_code.strip()
    if not configured:
        configured = getattr(settings, "BUNDLE_ID", "") or ""
    if configured and configured != "xxxx":
        try:
            found = client.describe_workspace_bundles(BundleIds=[configured])
        except (ClientError, BotoCoreError) as exc:
            raise aws_error(exc) from exc
        bundles = found.get("Bundles") or []
        if not bundles:
            raise CloudApiError("The WorkSpaces bundle was not found in this region.")
        return bundles[0]

    bundles = []
    token = ""
    while True:
        kwargs = {"Owner": "AMAZON"}
        if token:
            kwargs["NextToken"] = token
        try:
            page = client.describe_workspace_bundles(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise aws_error(exc) from exc
        bundles.extend(page.get("Bundles") or [])
        token = page.get("NextToken") or ""
        if not token:
            break
    ranked = sorted(bundles, key=bundle_score, reverse=True)
    if not ranked or bundle_score(ranked[0]) == 0:
        raise CloudApiError("No Windows WorkSpaces bundle was found in this region.")
    return ranked[0]


def bundle_resources(bundle):
    compute = (bundle.get("ComputeType") or {}).get("Name")
    cpu, ram = COMPUTE_SPECS.get(compute, (None, None))
    system_disk = (bundle.get("RootStorage") or {}).get("Capacity") or 80
    data_disk = (bundle.get("UserStorage") or {}).get("Capacity") or 50
    return cpu, ram, system_disk, data_disk


def create_cloud_desktop(name, profile=None, directory_username=""):
    if not DIRECTORY_ID or DIRECTORY_ID == "xxxx":
        raise CloudApiError("Set DIRECTORY_ID in local settings to the WorkSpaces directory ID, such as d-xxxxxxxxxx.")
    if not directory_username:
        raise CloudApiError("Enter the WorkSpaces directory username. That user must already exist in the directory.")

    client = workspaces_client()
    bundle = choose_bundle(client, profile)
    cpu, ram, system_disk, data_disk = bundle_resources(bundle)
    try:
        payload = client.create_workspaces(
            Workspaces=[
                {
                    "DirectoryId": DIRECTORY_ID,
                    "UserName": directory_username,
                    "BundleId": bundle["BundleId"],
                    "WorkspaceName": name,
                    "WorkspaceProperties": {
                        "RunningMode": "AUTO_STOP",
                        "RunningModeAutoStopTimeoutInMinutes": 60,
                    },
                    "Tags": [{"Key": "Name", "Value": name}],
                }
            ]
        )
    except (ClientError, BotoCoreError) as exc:
        raise aws_error(exc) from exc

    error = failed_request(payload)
    pending = payload.get("PendingRequests") or []
    if error or not pending:
        raise error or CloudApiError("AWS did not return a WorkSpace ID.")
    return {
        "vm_id": pending[0]["WorkspaceId"],
        "cpu": cpu,
        "ram": ram,
        "system_disk": system_disk,
        "data_disk": data_disk,
    }


def delete_cloud_desktop(vm_id):
    if not vm_id:
        return
    _client, payload = call_workspaces("terminate_workspaces", TerminateWorkspaceRequests=[{"WorkspaceId": vm_id}])
    error = failed_request(payload)
    if error:
        raise error


def describe_desktops():
    workspaces = []
    token = ""
    while True:
        kwargs = {}
        if DIRECTORY_ID and DIRECTORY_ID != "xxxx":
            kwargs["DirectoryId"] = DIRECTORY_ID
        if token:
            kwargs["NextToken"] = token
        _client, payload = call_workspaces("describe_workspaces", **kwargs)
        workspaces.extend(payload.get("Workspaces") or [])
        token = payload.get("NextToken") or ""
        if not token:
            break
    return workspaces


def apply_desktop(workspace):
    vm_id = workspace.get("WorkspaceId")
    if not vm_id:
        return None

    vm, _created = VirtualMachine.objects.get_or_create(
        vm_id=vm_id,
        defaults={"region": REGION_ID, "status": "Creating"},
    )
    vm.name = vm.name or workspace.get("ComputerName") or workspace.get("UserName")
    vm.region = workspace.get("Region") or REGION_ID
    vm.directory_id = workspace.get("DirectoryId") or vm.directory_id
    vm.status = STATUS_FROM_CLOUD.get(workspace.get("State"), "Error")

    properties = workspace.get("WorkspaceProperties") or {}
    cpu, ram = COMPUTE_SPECS.get(properties.get("ComputeTypeName"), (None, None))
    if cpu is not None:
        vm.cpu = cpu
    if ram is not None:
        vm.ram = ram
    if properties.get("RootVolumeSizeGib"):
        vm.system_disk = properties["RootVolumeSizeGib"]
    if properties.get("UserVolumeSizeGib"):
        vm.data_disk = properties["UserVolumeSizeGib"]
    if workspace.get("IpAddress"):
        vm.private_ip = workspace["IpAddress"]

    bundle_id = workspace.get("BundleId")
    if bundle_id:
        profile = VMProfile.objects.filter(instance_type_code=bundle_id).first()
        if profile:
            vm.current_profile = profile

    username = workspace.get("UserName") or ""
    matched = Profile.objects.filter(eds_end_user_id=username).select_related("user").first()
    if matched is None and username:
        account = User.objects.filter(username=username).first()
        if account is not None:
            vm.user = account
    if matched is not None:
        vm.user = matched.user

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


def operate_desktop(request, vm, action, workspace_action, next_status, time_field):
    if not vm.vm_id:
        raise CloudApiError("This desktop has no WorkSpace ID yet.")
    argument = "StartWorkspaceRequests" if workspace_action == "start_workspaces" else "StopWorkspaceRequests"
    if workspace_action == "reboot_workspaces":
        argument = "RebootWorkspaceRequests"
    try:
        _client, payload = call_workspaces(workspace_action, **{argument: [{"WorkspaceId": vm.vm_id}]})
    except CloudApiError as exc:
        write_log(request, vm, "API Error", f"{exc.code}: {exc}")
        raise
    error = failed_request(payload)
    if error:
        write_log(request, vm, "API Error", f"{error.code}: {error}")
        raise error
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
        raise CloudApiError("This desktop has no WorkSpace ID yet.")
    directory_id = vm.directory_id or DIRECTORY_ID
    if not directory_id or directory_id == "xxxx":
        raise CloudApiError("This desktop has no WorkSpaces directory ID.")
    _client, directories = call_workspaces("describe_workspace_directories", DirectoryIds=[directory_id])
    found = directories.get("Directories") or []
    if not found or not found[0].get("RegistrationCode"):
        raise CloudApiError("AWS did not return a WorkSpaces registration code.")
    _client, described = call_workspaces("describe_workspaces", WorkspaceIds=[vm.vm_id])
    spaces = described.get("Workspaces") or []
    username = spaces[0].get("UserName") if spaces else ""
    code = found[0]["RegistrationCode"]
    return (
        "Amazon WorkSpaces\n"
        "Registration code: {}\n"
        "Username: {}\n"
        "Install the Amazon WorkSpaces client, enter the registration code, then sign in with the directory password.\n"
        "Client download: https://clients.amazonworkspaces.com/\n"
    ).format(code, username or "-")


def run_desktop_action(request, pk, action, workspace_action, next_status, time_field):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not can_operate(request.user, vm):
        return JsonResponse({"detail": "You cannot operate this desktop."}, status=403)
    try:
        operate_desktop(request, vm, action, workspace_action, next_status, time_field)
    except CloudApiError as exc:
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
    except CloudApiError as exc:
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
    return run_desktop_action(request, pk, "Start", "start_workspaces", "Starting", "last_start_time")


@require_POST
@login_required_json
def desktop_stop(request, pk):
    return run_desktop_action(request, pk, "Stop", "stop_workspaces", "Stopping", "last_stop_time")


@require_POST
@login_required_json
def desktop_restart(request, pk):
    return run_desktop_action(request, pk, "Restart", "reboot_workspaces", "Starting", "last_start_time")
