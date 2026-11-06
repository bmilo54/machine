import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.profiles.models import Profile
from apps.virtuals.choices import (
    DATA_DISK_MAX,
    DATA_DISK_MIN,
    DESKTOP_TYPE_SPECS,
    SYSTEM_DISK_MAX,
    SYSTEM_DISK_MIN,
    valid_disk_size,
)
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine

ACCESS_KEY_ID = settings.ACCESS_KEY_ID
ACCESS_KEY_SECRET = settings.ACCESS_KEY_SECRET
REGION_ID = settings.REGION_ID
DIRECTORY_ID = settings.DIRECTORY_ID

ECD_VERSION = "2020-09-30"
EDS_USER_VERSION = "2021-03-08"
WUYING_DOWNLOAD_URL = "https://www.alibabacloud.com/en/product/cloud-desktop/download-client"
WUYING_WEB_URL = "https://workspace.alibabacloud.com"
# International-site accounts keep Wuying users in Singapore; China-site accounts use eds-user.cn-shanghai.aliyuncs.com.
EDS_USER_HOST = getattr(settings, "EDS_USER_HOST", "eds-user.ap-southeast-1.aliyuncs.com")
WINDOWS_11_PRO_IMAGE_ID = "desktopimage-windows-11-64-asp"
WINDOWS_11_PRO_GPU_IMAGE_ID = "desktopimage-windows-11-64-asp-gpu"
WINDOWS_LANGUAGE = "en-US"
DEFAULT_DESKTOP_TYPE = "eds.general.4c8g"
FALLBACK_POLICY_GROUP_ID = "system-all-enabled-policy"
STATUS_FROM_CLOUD = {
    "Running": "Running",
    "Stopped": "Stopped",
    "Starting": "Starting",
    "Stopping": "Stopping",
    "Pending": "Creating",
    "Rebuilding": "Creating",
    "Updating": "Upgrading",
    "Upgrading": "Upgrading",
    "Expired": "Error",
}
RELEASED_STATUSES = {"Deleting", "Deleted", "Releasing", "Released"}
LIVE_CLOUD_STATUSES = {
    "Running",
    "Stopped",
    "Starting",
    "Stopping",
    "Pending",
    "Rebuilding",
    "Updating",
    "Upgrading",
}


def mark_desktop_released(vm_id):
    if vm_id:
        cache.set("ecd-released:{}".format(vm_id), True, 2 * 60 * 60)


def desktop_was_released(vm_id):
    if not vm_id:
        return False
    if cache.get("ecd-released:{}".format(vm_id)):
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    released = ActivityLog.objects.filter(
        cloud_id=vm_id,
        action="Delete",
        created__gte=cutoff,
    ).exists()
    if released:
        cache.set("ecd-released:{}".format(vm_id), True, 2 * 60 * 60)
    return released


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
    return call_alibaba("ecd", ECD_VERSION, action, extra)


def call_eds_user(action, extra=None):
    return call_alibaba("eds-user", EDS_USER_VERSION, action, extra, host=EDS_USER_HOST)


def call_alibaba(service, version, action, extra=None, host=None):
    params = {
        "Action": action,
        "Format": "JSON",
        "Version": version,
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
    host = host or f"{service}.{REGION_ID}.aliyuncs.com"
    request = Request(f"https://{host}/?{query}")

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


def list_bundles():
    bundles = []
    next_token = ""
    for _ in range(20):
        extra = {"MaxResults": 100}
        if next_token:
            extra["NextToken"] = next_token
        payload = call_ecd("DescribeBundles", extra)
        bundles.extend(payload.get("Bundles") or [])
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
    return bundles


def bundle_disks(bundle):
    disks = {}
    for disk in bundle.get("Disks") or []:
        if disk.get("DiskSize") not in (None, ""):
            disks[disk.get("DiskType")] = int(disk["DiskSize"])
    return disks


def resolve_desktop_spec(profile):
    desktop_type = DEFAULT_DESKTOP_TYPE
    if profile and profile.instance_type_code and profile.instance_type_code.strip():
        desktop_type = profile.instance_type_code.strip()
    spec = DESKTOP_TYPE_SPECS.get(desktop_type)
    if not spec:
        raise AlibabaApiError(
            "This profile has no desktop type Alibaba sells. Edit the profile and pick a desktop type."
        )

    system_disk = int(profile.system_disk or 80) if profile else 80
    data_disk = int(profile.data_disk or 0) if profile else 0
    return desktop_type, spec, system_disk, data_disk


def find_windows_bundle(bundles, image_id, desktop_type, system_disk, data_disk):
    for bundle in bundles:
        if bundle.get("ImageId") != image_id:
            continue
        if bundle.get("DesktopType") != desktop_type:
            continue
        if bundle.get("Language") != WINDOWS_LANGUAGE:
            continue
        disks = bundle_disks(bundle)
        if disks.get("SYSTEM") == system_disk and disks.get("DATA", 0) == data_disk:
            return bundle.get("BundleId") or ""
    return ""


def ensure_windows_bundle(bundles, image_id, desktop_type, system_disk, data_disk):
    bundle_id = find_windows_bundle(bundles, image_id, desktop_type, system_disk, data_disk)
    if bundle_id:
        return bundle_id

    payload = call_ecd(
        "CreateBundle",
        {
            "BundleName": "win11-en-{}-s{}-d{}".format(
                desktop_type.split(".", 1)[-1].replace(".", "-"),
                system_disk,
                data_disk,
            ),
            "ImageId": image_id,
            "DesktopType": desktop_type,
            "RootDiskSizeGib": system_disk,
            "UserDiskSizeGib.1": data_disk,
            "Language": WINDOWS_LANGUAGE,
        },
    )
    bundle_id = (payload.get("BundleId") or "").strip()
    if not bundle_id:
        raise AlibabaApiError("Alibaba did not return a Windows 11 template ID.")
    return bundle_id


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


def desktop_policy_id():
    policy_id = (getattr(settings, "POLICY_GROUP_ID", "") or "").strip()
    if not policy_id or policy_id == "xxxx":
        return FALLBACK_POLICY_GROUP_ID
    return policy_id


def desktop_policy_name():
    policy_id = desktop_policy_id()
    cache_key = "ecd-policy-name:{}".format(policy_id)
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        payload = call_ecd("DescribePolicyGroups", {"PolicyGroupId": policy_id, "MaxResults": 20})
    except AlibabaApiError:
        return policy_id
    for group in payload.get("DescribePolicyGroups") or []:
        if group.get("PolicyGroupId") == policy_id:
            name = group.get("Name") or policy_id
            cache.set(cache_key, name, 60 * 60)
            return name
    return policy_id


def listed_network_packages(payload):
    raw = payload.get("NetworkPackages") or payload.get("NetworkPackage") or []
    if isinstance(raw, dict):
        raw = raw.get("NetworkPackage") or raw.get("NetworkPackages") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [item for item in raw if isinstance(item, dict)]


def office_network_package():
    payload = call_ecd("DescribeNetworkPackages", {"OfficeSiteId": DIRECTORY_ID, "MaxResults": 20})
    for package in listed_network_packages(payload):
        site = package.get("OfficeSiteId") or ""
        if site and site != DIRECTORY_ID:
            continue
        if package.get("NetworkPackageId"):
            return package
    return None


def bandwidth_label_from(mode, mbps, charge):
    if mode != "premium":
        return "No premium"
    charge_name = "by bandwidth" if charge == "PayByBandwidth" else "by data transfer"
    return "Premium {} Mbps, {}".format(mbps, charge_name)


def bandwidth_state():
    package = office_network_package()
    if not package:
        return {"mode": "basic", "mbps": 10, "charge": "PayByTraffic", "label": "No premium"}
    charge = package.get("InternetChargeType") or "PayByTraffic"
    if charge not in ("PayByTraffic", "PayByBandwidth"):
        charge = "PayByTraffic"
    mbps = int(package.get("Bandwidth") or 10)
    return {
        "mode": "premium",
        "mbps": mbps,
        "charge": charge,
        "label": bandwidth_label_from("premium", mbps, charge),
        "package_id": package.get("NetworkPackageId") or "",
    }


def create_premium_bandwidth(peak, charge):
    payload = call_ecd("CreateNetworkPackage", {
        "Bandwidth": peak,
        "OfficeSiteId": DIRECTORY_ID,
        "PayType": "PostPaid",
        "InternetChargeType": charge,
        "AutoPay": "true",
    })
    return payload.get("NetworkPackageId") or ""


def network_package_by_id(package_id):
    payload = call_ecd("DescribeNetworkPackages", {
        "NetworkPackageId.1": package_id,
        "MaxResults": 20,
    })
    for package in listed_network_packages(payload):
        if package.get("NetworkPackageId") == package_id:
            return package
    return None


def status_blocks_action(exc):
    return "does not support" in str(exc).lower()


def release_network_package(package_id):
    """Unbind the plan, then delete it. Alibaba rejects both calls while the plan is still changing."""
    last_error = None
    unbound = False
    for _ in range(6):
        current = network_package_by_id(package_id)
        if current is None or current.get("NetworkPackageStatus") == "Released":
            return
        status = current.get("NetworkPackageStatus") or ""
        if status == "Releasing":
            time.sleep(2)
            continue
        if status == "InUse" and not unbound:
            try:
                call_ecd("DissociateNetworkPackage", {"NetworkPackageId": package_id})
                unbound = True
            except AlibabaApiError as exc:
                last_error = exc
                if not status_blocks_action(exc):
                    raise
            time.sleep(2)
            continue
        try:
            call_ecd("DeleteNetworkPackages", {"NetworkPackageId.1": package_id})
        except AlibabaApiError as exc:
            last_error = exc
            if not status_blocks_action(exc):
                raise
            time.sleep(2)
            continue
        time.sleep(1)
        if network_package_by_id(package_id) is None:
            return
    if last_error and not status_blocks_action(last_error):
        raise last_error
    raise AlibabaApiError(
        "Alibaba is still updating this bandwidth plan, so it cannot be removed yet. Wait a minute and save again."
    )


def set_office_bandwidth(mode, mbps, charge):
    if mode == "premium":
        peak = int(mbps or 0)
        if peak < 2 or peak > 200:
            raise AlibabaApiError("Premium bandwidth must be 2 to 200 Mbps.")
        if charge not in ("PayByTraffic", "PayByBandwidth"):
            charge = "PayByTraffic"
    else:
        peak = 0
        charge = "PayByTraffic"
    current = office_network_package()
    if mode != "premium":
        if current and current.get("NetworkPackageId"):
            release_network_package(current["NetworkPackageId"])
        return "No premium"
    if current is None:
        create_premium_bandwidth(peak, charge)
        return bandwidth_label_from("premium", peak, charge)
    package_id = current.get("NetworkPackageId")
    current_charge = current.get("InternetChargeType") or "PayByTraffic"
    current_peak = int(current.get("Bandwidth") or 0)
    if current_charge != charge:
        release_network_package(package_id)
        create_premium_bandwidth(peak, charge)
        return bandwidth_label_from("premium", peak, charge)
    if current_peak != peak:
        call_ecd("ModifyNetworkPackageBandwidth", {
            "NetworkPackageId": package_id,
            "Bandwidth": peak,
            "AutoPay": "true",
        })
    return bandwidth_label_from("premium", peak, charge)


def create_cloud_desktop(name, profile=None, end_user_id=""):
    if not DIRECTORY_ID or DIRECTORY_ID == "xxxx":
        raise AlibabaApiError("Set DIRECTORY_ID in local settings to the Hangzhou office network.")

    desktop_type, spec, system_disk, data_disk = resolve_desktop_spec(profile)
    if not valid_disk_size(system_disk, SYSTEM_DISK_MIN, SYSTEM_DISK_MAX):
        raise AlibabaApiError(
            "This profile asks for a {} GiB system disk. Use 60 to 2040 GiB, in steps of 10. It must be greater than the 50 GiB image.".format(
                system_disk,
            )
        )
    if not valid_disk_size(data_disk, DATA_DISK_MIN, DATA_DISK_MAX, allow_zero=True):
        raise AlibabaApiError(
            "This profile asks for a {} GiB data disk. Use 50 to 2040 GiB, in steps of 10.".format(
                data_disk,
            )
        )

    image_id = WINDOWS_11_PRO_GPU_IMAGE_ID if spec["gpu"] else WINDOWS_11_PRO_IMAGE_ID
    bundle_id = ensure_windows_bundle(list_bundles(), image_id, desktop_type, system_disk, data_disk)

    extra = {
        "DesktopName": name,
        "Amount": 1,
        "OfficeSiteId": DIRECTORY_ID,
        "PolicyGroupId": desktop_policy_id(),
        "ChargeType": "PostPaid",
        "BundleId": bundle_id,
    }
    if end_user_id:
        extra["EndUserId.1"] = end_user_id

    payload = call_ecd("CreateDesktops", extra)
    return {
        "vm_id": desktop_id_from(payload),
        "cpu": spec["cpu"],
        "ram": spec["ram"],
        "desktop_type": desktop_type,
        "system_disk": system_disk,
        "data_disk": data_disk,
    }


BUSY_STATUSES = ("Creating", "Starting", "Stopping", "Updating", "Upgrading")
HOURS_IN_MONTH = 730


def desktop_hourly_price(desktop_type, system_disk, data_disk):
    if not desktop_type:
        return None
    cache_key = "ecd-price:v2:{}:{}:{}".format(desktop_type, system_disk or 0, data_disk or 0)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    payload = call_ecd("DescribePrice", {
        "ResourceType": "Desktop",
        "InstanceType": desktop_type,
        "RootDiskSizeGib": system_disk or 80,
        "UserDiskSizeGib": data_disk or 0,
        "OsType": "Windows",
        "PeriodUnit": "Hour",
        "Period": 1,
        "Amount": 1,
    })
    price = ((payload.get("PriceInfo") or {}).get("Price") or {})
    trade = price.get("TradePrice")
    if trade in (None, ""):
        return None
    modules = []
    lines = price.get("OrderLines") or {}
    if isinstance(lines, list):
        line = lines[0] if lines else {}
    elif isinstance(lines, dict) and lines:
        line = next(iter(lines.values()))
    else:
        line = {}
    if isinstance(line, list):
        line = line[0] if line else {}
    modules = line.get("moduleInstance") or []

    def module_fee(code):
        for module in modules:
            if module.get("moduleCode") != code:
                continue
            fee = module.get("totalProductFee")
            if fee in (None, ""):
                return 0.0
            return float(fee) / 100
        return 0.0

    disk_hourly = module_fee("SystemDiskSize") + module_fee("DataDiskSize")
    compute_hourly = module_fee("DesktopType")
    hourly = float(trade)
    if compute_hourly <= 0 and disk_hourly <= 0:
        compute_hourly = hourly
        disk_hourly = 0.0
    quoted = {
        "hourly": hourly,
        "monthly": hourly * HOURS_IN_MONTH,
        "disk_hourly": disk_hourly,
        "compute_hourly": compute_hourly,
        "currency": price.get("Currency") or "USD",
    }
    cache.set(cache_key, quoted, 6 * 60 * 60)
    return quoted


def delete_cloud_desktop(vm_id):
    if not vm_id:
        return
    call_ecd("DeleteDesktops", {"DesktopId.1": vm_id})


def assign_desktop_user(vm_id, end_user_id=""):
    extra = {"DesktopId": vm_id}
    if end_user_id:
        extra["EndUserId.1"] = end_user_id
    call_ecd("ModifyEntitlement", extra)


def modify_desktop_spec(vm_id, desktop_type, system_disk=None, data_disk=None):
    extra = {"DesktopId": vm_id, "DesktopType": desktop_type}
    if system_disk is not None:
        extra["RootDiskSizeGib"] = int(system_disk)
    if data_disk is not None:
        extra["UserDiskSizeGib"] = int(data_disk)
    call_ecd("ModifyDesktopSpec", extra)


WUYING_USER_STATUS = {0: "Active", 9: "Locked"}


def describe_wuying_users():
    users = {}
    next_token = ""
    for _ in range(20):
        extra = {"MaxResults": 100}
        if next_token:
            extra["NextToken"] = next_token
        payload = call_eds_user("DescribeUsers", extra)
        for user in payload.get("Users") or []:
            end_user_id = user.get("EndUserId")
            if not end_user_id:
                continue
            status = user.get("Status")
            try:
                status = int(status)
            except (TypeError, ValueError):
                pass
            users[end_user_id] = {
                "end_user_id": end_user_id,
                "email": user.get("Email") or "",
                "phone": user.get("Phone") or "",
                "name": user.get("RealNickName") or "",
                "status": WUYING_USER_STATUS.get(status, str(status) if status not in (None, "") else "Unknown"),
            }
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
    return users


def remove_wuying_user(end_user_id):
    if not end_user_id or end_user_id not in describe_wuying_users():
        return
    try:
        payload = call_eds_user("RemoveUsers", {"Users.1": end_user_id})
    except AlibabaApiError as exc:
        if "NotFound" in (exc.code or ""):
            return
        raise
    result = payload.get("RemoveUsersResult") or {}
    failed = result.get("FailedUsers") or []
    if failed:
        first = failed[0]
        raise AlibabaApiError(
            first.get("ErrorMessage") or "Alibaba did not delete the Wuying user.",
            first.get("ErrorCode") or "",
        )


def change_wuying_password(end_user_id, new_password):
    call_eds_user("ChangeUserPassword", {"EndUserId": end_user_id, "NewPassword": new_password})


def create_wuying_user(end_user_id, email, password, display_name=""):
    extra = {
        "Users.1.EndUserId": end_user_id,
        "Users.1.Email": email,
        "Users.1.OwnerType": "CreateFromManager",
        "Users.1.Password": password,
        "Password": password,
    }
    if display_name:
        extra["Users.1.RealNickName"] = display_name
    payload = call_eds_user("CreateUsers", extra)
    result = payload.get("CreateResult") or {}
    failed = result.get("FailedUsers") or []
    if failed or payload.get("AllSucceed") is False:
        first = failed[0] if failed else {}
        raise AlibabaApiError(
            first.get("ErrorMessage") or "Alibaba did not create the Wuying user.",
            first.get("ErrorCode") or "",
        )


def refresh_desktop(vm):
    if vm is None or not vm.vm_id:
        return vm
    payload = call_ecd("DescribeDesktops", {"DesktopId.1": vm.vm_id, "MaxResults": 1})
    desktops = payload.get("Desktops") or []
    if desktops:
        saved = apply_desktop(desktops[0])
        if saved is None:
            return None
        vm.refresh_from_db()
    return vm


def describe_recordings(start, end, desktop_id=""):
    extra = {
        "MaxResults": 100,
        "NeedSignedUrl": True,
        "SignedUrlExpireMinutes": 60,
        "StandardStartTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "StandardEndTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if desktop_id:
        extra["DesktopId"] = desktop_id
    recordings = []
    next_token = ""
    for _ in range(5):
        params = dict(extra)
        if next_token:
            params["NextToken"] = next_token
        payload = call_ecd("DescribeRecordings", params)
        recordings.extend(payload.get("Recordings") or [])
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
    return recordings


def recording_signed_url(file_path):
    cache_key = "ecd-recording-url:{}".format(hashlib.sha256(file_path.encode("utf-8")).hexdigest())
    cached = cache.get(cache_key)
    if cached:
        return cached
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    for item in describe_recordings(start, end):
        if (item.get("FilePath") or "") != file_path:
            continue
        signed_url = item.get("SignedUrl") or ""
        if signed_url:
            cache.set(cache_key, signed_url, 50 * 60)
        return signed_url
    return ""


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


SIGNIN_EVENT_LABELS = {
    "CLIENT_LOGIN": "Signed in",
    "DESKTOP_CONNECT": "Opened the computer",
    "DESKTOP_DISCONNECT": "Left the computer",
}


def parse_alibaba_time(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def describe_signin_events(start, end, end_user_id=""):
    extra = {
        "StartTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "EndTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "MaxResults": 100,
        "EventTypes.1": "CLIENT_LOGIN",
        "EventTypes.2": "DESKTOP_CONNECT",
        "EventTypes.3": "DESKTOP_DISCONNECT",
    }
    if end_user_id:
        extra["EndUserId"] = end_user_id

    events = []
    next_token = ""
    for _ in range(5):
        params = dict(extra)
        if next_token:
            params["NextToken"] = next_token
        payload = call_ecd("DescribeClientEvents", params)
        events.extend(payload.get("Events") or [])
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
    return events


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
    cloud_status = (desktop.get("DesktopStatus") or "").strip()
    if cloud_status in RELEASED_STATUSES or (
        desktop_was_released(vm_id) and cloud_status not in LIVE_CLOUD_STATUSES
    ):
        VirtualMachine.objects.filter(vm_id=vm_id).delete()
        return None

    vm, _created = VirtualMachine.objects.get_or_create(
        vm_id=vm_id,
        defaults={"region": REGION_ID, "status": "Creating"},
    )
    vm.name = desktop.get("DesktopName") or vm.name
    vm.region = REGION_ID
    vm.directory_id = desktop.get("DirectoryId") or desktop.get("OfficeSiteId") or vm.directory_id
    vm.status = STATUS_FROM_CLOUD.get(cloud_status, "Error")
    if vm.status == "Error":
        vm.status_detail = "Alibaba status: {}.".format(cloud_status or "not reported")
    else:
        vm.status_detail = ""
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
        vm.desktop_type = desktop_type
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


def write_log(request, vm, action, details="", old_value="", new_value="", desktop_name="", cloud_id=""):
    user = request.user if getattr(request.user, "is_authenticated", False) else None
    if vm is not None:
        desktop_name = desktop_name or (vm.name or "")
        cloud_id = cloud_id or (vm.vm_id or "")
    ActivityLog.objects.create(
        actor=user,
        virtual_machine=vm,
        desktop_name=(desktop_name or "")[:255],
        cloud_id=(cloud_id or "")[:200],
        action=action or "",
        old_value=old_value or "",
        new_value=new_value or "",
        details=details or "",
        ip_address=client_ip(request),
        created_by=user,
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
    write_log(request, vm, action, "{} requested.".format(action))
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
