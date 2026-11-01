import re
import secrets
import string
from datetime import timedelta

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator as DjangoUnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from apps.profiles.models import Profile
from apps.virtuals.choices import (
    DATA_DISK_MAX,
    DATA_DISK_MIN,
    DESKTOP_TYPE_CHOICES,
    DESKTOP_TYPE_SPECS,
    DISK_STEP,
    SPEC_CHANGE_WAIT_MINUTES,
    SPEC_DATA_MAX,
    SPEC_DATA_MIN,
    SPEC_SYSTEM_MAX,
    SPEC_SYSTEM_MIN,
    SYSTEM_DISK_MAX,
    SYSTEM_DISK_MIN,
    desktop_type_family,
    family_type_choices,
    valid_disk_size,
    valid_grown_disk,
)
from apps.virtuals.models import ActivityLog, VMProfile, VirtualMachine


def end_user_queryset():
    return User.objects.filter(groups__isnull=True).distinct().order_by("username")

class DashboardLoginForm(AuthenticationForm):
    username = forms.CharField(label="Username")

    def __init__(self, *args, **kwargs):
        super(DashboardLoginForm, self).__init__(*args, **kwargs)

        self.fields['username'].widget.attrs.update({
            'class': 'login-input',
            'placeholder': 'Your username',
            'autofocus': True,
        })
        self.fields['password'].widget.attrs.update({
            'class': 'login-input login-input-password',
            'placeholder': 'Your password',
            'id': 'password',
        })


class DesktopCreateForm(forms.ModelForm):
    class Meta:
        model = VirtualMachine
        fields = ("name", "user", "current_profile")

    def __init__(self, *args, **kwargs):
        super(DesktopCreateForm, self).__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["name"].label = "Desktop name"
        self.fields["name"].help_text = "Windows 11 Pro in China (Hangzhou). Billing is pay-as-you-go."
        self.fields["user"].required = False
        self.fields["user"].queryset = end_user_queryset()
        self.fields["user"].help_text = "Optional. The desktop is assigned to this user in the dashboard."
        self.fields["current_profile"].required = False
        self.fields["current_profile"].queryset = VMProfile.objects.order_by("name")
        self.fields["current_profile"].help_text = "Leave empty for 4 vCPU, 8 GB, an 80 GB system disk, and no data disk."

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if len(name) > 64:
            raise forms.ValidationError("Use 64 characters or fewer.")
        if name.lower().startswith(("http://", "https://")) or not name[0].isalpha():
            raise forms.ValidationError("Start the name with a letter.")
        return name


class DesktopSpecForm(forms.Form):
    desktop_type = forms.ChoiceField(
        label="Desktop type",
        choices=[],
        help_text="CPU, memory, and GPU change together. A computer stays in its own family.",
    )
    system_disk = forms.IntegerField(
        label="System disk (GiB)",
        help_text="Can only grow, in steps of 10 GiB. Alibaba accepts 80 to 500 GiB when you change it.",
    )
    data_disk = forms.IntegerField(
        label="Data disk (GiB)",
        help_text="Can only grow, in steps of 10 GiB. Leave 0 if this computer has no data disk.",
    )

    def __init__(self, desktop, *args, **kwargs):
        self.desktop = desktop
        super(DesktopSpecForm, self).__init__(*args, **kwargs)
        family = desktop_type_family(desktop.desktop_type)
        choices = family_type_choices(desktop.desktop_type)
        if choices:
            self.fields["desktop_type"].choices = choices
            self.fields["desktop_type"].initial = desktop.desktop_type
        else:
            self.fields["desktop_type"].choices = [("", "Sync this desktop from Alibaba first")]
            self.fields["desktop_type"].disabled = True
        data_floor = SPEC_DATA_MIN.get(family, SPEC_DATA_MIN["office"])
        self.fields["system_disk"].initial = desktop.system_disk
        self.fields["data_disk"].initial = desktop.data_disk
        self.fields["system_disk"].widget.attrs.update({
            "step": DISK_STEP,
            "min": desktop.system_disk or SPEC_SYSTEM_MIN,
            "max": max(SPEC_SYSTEM_MAX, desktop.system_disk or 0),
        })
        self.fields["data_disk"].widget.attrs.update({
            "step": DISK_STEP,
            "min": desktop.data_disk or 0,
            "max": max(SPEC_DATA_MAX, desktop.data_disk or 0),
        })
        self.fields["data_disk"].help_text = (
            "Can only grow, in steps of 10 GiB. A new data disk starts at {} GiB and can reach {} GiB. "
            "Keep the current size, including 0, to leave the disk unchanged."
        ).format(data_floor, SPEC_DATA_MAX)

    def clean(self):
        cleaned = super(DesktopSpecForm, self).clean()
        desktop = self.desktop
        family = desktop_type_family(desktop.desktop_type)
        type_id = cleaned.get("desktop_type") or desktop.desktop_type
        system_disk = cleaned.get("system_disk")
        data_disk = cleaned.get("data_disk")
        if system_disk is None or data_disk is None:
            return cleaned
        if not desktop.vm_id:
            raise forms.ValidationError("This desktop has no Alibaba computer yet.")
        if not desktop.desktop_type or desktop.desktop_type not in DESKTOP_TYPE_SPECS:
            raise forms.ValidationError("Sync this desktop from Alibaba so its type is known, then change it.")
        if desktop.status != "Stopped":
            raise forms.ValidationError(
                "Stop this computer first. It is currently {}.".format(desktop.get_status_display())
            )
        if type_id != desktop.desktop_type and desktop_type_family(type_id) != family:
            self.add_error("desktop_type", "Stay in the same family. General Office, Graphics Workstation (Basic), and Professional Workstation each stay in their own list. Cloud Computer Graphics can only move to Graphics Workstation (Ultimate).")
        data_floor = SPEC_DATA_MIN.get(family, SPEC_DATA_MIN["office"])
        if not valid_grown_disk(system_disk, desktop.system_disk, SPEC_SYSTEM_MIN, SPEC_SYSTEM_MAX):
            self.add_error(
                "system_disk",
                "Keep {} GiB, or grow it to {}–{} GiB in steps of 10. Disks cannot shrink.".format(
                    desktop.system_disk, max(SPEC_SYSTEM_MIN, desktop.system_disk or 0), SPEC_SYSTEM_MAX,
                ),
            )
        if not valid_grown_disk(data_disk, desktop.data_disk, data_floor, SPEC_DATA_MAX):
            self.add_error(
                "data_disk",
                "Keep {} GiB, or grow it from {} GiB up to {} GiB in steps of 10. Disks cannot shrink.".format(
                    desktop.data_disk, max(data_floor, (desktop.data_disk or 0) + DISK_STEP if desktop.data_disk else data_floor), SPEC_DATA_MAX,
                ),
            )
        changed = (
            type_id != desktop.desktop_type
            or system_disk != desktop.system_disk
            or data_disk != desktop.data_disk
        )
        if not changed and not self.errors:
            raise forms.ValidationError("Change the desktop type or a disk size.")
        if changed and desktop.pk:
            recent = ActivityLog.objects.filter(virtual_machine=desktop, action="Resize").order_by("-created").first()
            if recent and timezone.now() - recent.created < timedelta(minutes=SPEC_CHANGE_WAIT_MINUTES):
                raise forms.ValidationError("Wait 5 minutes between specification changes.")
        cleaned["desktop_type"] = type_id
        return cleaned


class VirtualMachineForm(forms.ModelForm):
    class Meta:
        model = VirtualMachine
        exclude = (
            'created_by',
            'modified_by',
            'created',
            'modified',
            'last_start_time',
            'last_stop_time',
        )

    def __init__(self, *args, **kwargs):
        super(VirtualMachineForm, self).__init__(*args, **kwargs)
        users = end_user_queryset()
        if self.instance.pk and self.instance.user_id and self.instance.user_id not in users.values_list("pk", flat=True):
            users = User.objects.filter(pk=self.instance.user_id) | users
        self.fields['user'].queryset = users.order_by("username")
        self.fields['current_profile'].queryset = VMProfile.objects.order_by('name')


DESKTOP_TYPE_GUIDE = [
    {
        "title": "General Office.",
        "text": "Everyday work: browser, Office, email, and light applications. No GPU. This is the Singapore office type and the lower price.",
    },
    {
        "title": "Cloud Computer Graphics.",
        "text": "One size: 8 vCPU, 16 GiB memory, 4 GiB GPU. The GPU is an NVIDIA RTX 5880. It can later move up to Graphics Workstation (Ultimate).",
    },
    {
        "title": "Graphics Workstation (Ultimate).",
        "text": "Design, 3D, and video. The GPU is an NVIDIA RTX 5880, from 8 to 24 GiB.",
    },
    {
        "title": "Professional Workstation.",
        "text": "Heavy GPU and AI work. The GPU is an NVIDIA RTX 5880 with 48 GiB. Highest price. It cannot be changed into the other graphics families.",
    },
    {
        "title": "Graphics Workstation (Basic).",
        "text": "The lower graphics sizes. The GPU is an NVIDIA T4, from 2 to 16 GiB. It cannot be changed into an RTX 5880 computer.",
    },
]

SYSTEM_DISK_GUIDE = [
    {
        "title": "What it stores.",
        "text": "Windows and installed programs. User files can also live here, but a data disk keeps them separate.",
    },
    {
        "title": "Size.",
        "text": "60 to 2040 GiB, in steps of 10. Windows 11 Pro is a 50 GiB image, so the disk must be greater than 50 GiB.",
    },
    {
        "title": "After purchase.",
        "text": "The disk can only grow. A specification change accepts 80 to 500 GiB.",
    },
]

DATA_DISK_GUIDE = [
    {
        "title": "What it stores.",
        "text": "Extra space for files. It does not hold Windows. 0 means the computer has no data disk.",
    },
    {
        "title": "Size.",
        "text": "Use 0 for no data disk. A custom data disk is 50 to 2040 GiB, in steps of 10.",
    },
    {
        "title": "After purchase.",
        "text": "The disk can only grow, up to 1020 GiB. A new data disk starts at 20 GiB on an office computer and 40 GiB on a graphics computer.",
    },
]


class VMProfileForm(forms.ModelForm):
    instance_type_code = forms.ChoiceField(
        label="Desktop type",
        choices=DESKTOP_TYPE_CHOICES,
        initial="eds.general.4c8g",
        help_text="Same list as the Singapore console. Cloud Computer Graphics, Graphics Workstation (Ultimate), and Professional Workstation use an NVIDIA RTX 5880. Graphics Workstation (Basic) uses an NVIDIA T4.",
    )
    system_disk = forms.IntegerField(
        label="System disk (GiB)",
        min_value=SYSTEM_DISK_MIN,
        max_value=SYSTEM_DISK_MAX,
        initial=80,
        widget=forms.NumberInput(attrs={"step": DISK_STEP, "min": SYSTEM_DISK_MIN, "max": SYSTEM_DISK_MAX}),
        help_text="Custom purchase: 60 to 2040 GiB, in steps of 10. Windows 11 Pro is a 50 GiB image, so the disk must be greater than 50 GiB.",
    )
    data_disk = forms.IntegerField(
        label="Data disk (GiB)",
        min_value=0,
        max_value=DATA_DISK_MAX,
        initial=0,
        widget=forms.NumberInput(attrs={"step": DISK_STEP, "min": 0, "max": DATA_DISK_MAX}),
        help_text="Use 0 for no data disk, or 50 to 2040 GiB in steps of 10.",
    )

    def __init__(self, *args, **kwargs):
        super(VMProfileForm, self).__init__(*args, **kwargs)
        self.fields["instance_type_code"].guide = DESKTOP_TYPE_GUIDE
        self.fields["system_disk"].guide = SYSTEM_DISK_GUIDE
        self.fields["data_disk"].guide = DATA_DISK_GUIDE

    class Meta:
        model = VMProfile
        fields = ('name', 'instance_type_code', 'system_disk', 'data_disk')

    def clean_system_disk(self):
        size = self.cleaned_data["system_disk"]
        if not valid_disk_size(size, SYSTEM_DISK_MIN, SYSTEM_DISK_MAX):
            raise forms.ValidationError("Use 60 to 2040 GiB, in steps of 10. It must be greater than the 50 GiB image.")
        return size

    def clean_data_disk(self):
        size = self.cleaned_data["data_disk"]
        if not valid_disk_size(size, DATA_DISK_MIN, DATA_DISK_MAX, allow_zero=True):
            raise forms.ValidationError("Use 0 for no data disk, or 50 to 2040 GiB in steps of 10.")
        return size

    def save(self, commit=True):
        profile = super(VMProfileForm, self).save(commit=False)
        spec = DESKTOP_TYPE_SPECS[profile.instance_type_code]
        profile.cpu = spec["cpu"]
        profile.ram = spec["ram"]
        profile.gpu = spec["gpu"]
        if commit:
            profile.save()
        return profile


WUYING_MODE_CHOICES = [
    ("create", "Create a new Wuying user in Alibaba"),
    ("link", "Link a Wuying user that already exists"),
]


class DashboardUserCreateForm(forms.Form):
    username = forms.CharField(
        label="Username",
        max_length=150,
        help_text="Used to sign in to this dashboard. 150 characters or fewer.",
    )
    first_name = forms.CharField(label="First name", required=False, max_length=150)
    last_name = forms.CharField(label="Last name", required=False, max_length=150)
    email = forms.EmailField(
        label="Email",
        required=False,
        help_text="Alibaba sends desktop notices here when you create a new Wuying user.",
    )
    password = forms.CharField(
        label="Dashboard password",
        widget=forms.PasswordInput(render_value=False),
        help_text="Password for this dashboard. It is separate from the Wuying password.",
    )
    confirm_password = forms.CharField(
        label="Type the dashboard password again",
        widget=forms.PasswordInput(render_value=False),
    )
    wuying_mode = forms.ChoiceField(
        label="Wuying account",
        choices=WUYING_MODE_CHOICES,
        initial="create",
    )
    eds_end_user_id = forms.CharField(
        label="Wuying username",
        max_length=24,
        help_text="3 to 24 lowercase letters, digits, or underscores. The user signs in to Wuying with this.",
    )
    wuying_password = forms.CharField(
        label="Wuying password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave empty and the dashboard makes a 12 character password. It is shown on the Users page.",
    )
    display_name = forms.CharField(label="Wuying display name", required=False, max_length=64)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        DjangoUnicodeUsernameValidator()(username)
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def clean_eds_end_user_id(self):
        end_user_id = (self.cleaned_data.get("eds_end_user_id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_]{3,24}", end_user_id):
            raise forms.ValidationError("Use 3 to 24 lowercase letters, digits, or underscores.")
        if Profile.objects.filter(eds_end_user_id=end_user_id).exists():
            raise forms.ValidationError("This Wuying username is already linked to a dashboard user.")
        return end_user_id

    def clean(self):
        cleaned = super(DashboardUserCreateForm, self).clean()
        password = cleaned.get("password") or ""
        if password and password != cleaned.get("confirm_password"):
            self.add_error("confirm_password", "The two dashboard passwords do not match.")
        if password:
            try:
                validate_password(password, User(username=cleaned.get("username") or "", email=cleaned.get("email") or ""))
            except DjangoValidationError as exc:
                self.add_error("password", exc.messages)
        if cleaned.get("wuying_mode") != "create":
            return cleaned
        if not cleaned.get("email"):
            self.add_error("email", "Alibaba needs an email for a new Wuying user.")
        if not cleaned.get("wuying_password"):
            cleaned["wuying_password"] = generate_wuying_password()
        elif not wuying_password_ok(cleaned["wuying_password"]):
            self.add_error("wuying_password", WUYING_PASSWORD_RULE)
        return cleaned


class DashboardUserUpdateForm(forms.ModelForm):
    password = forms.CharField(
        label="New dashboard password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password.",
    )
    confirm_password = forms.CharField(
        label="Type the new dashboard password again",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )
    eds_end_user_id = forms.CharField(
        label="Wuying username",
        required=False,
        max_length=24,
        help_text="3 to 24 lowercase letters, digits, or underscores. Leave empty if this account has no Wuying login.",
    )

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super(DashboardUserUpdateForm, self).__init__(*args, **kwargs)
        profile = Profile.objects.filter(user=self.instance).first()
        if profile and profile.eds_end_user_id:
            self.fields["eds_end_user_id"].initial = profile.eds_end_user_id

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        DjangoUnicodeUsernameValidator()(username)
        if User.objects.exclude(pk=self.instance.pk).filter(username=username).exists():
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def clean_eds_end_user_id(self):
        end_user_id = (self.cleaned_data.get("eds_end_user_id") or "").strip()
        if not end_user_id:
            return ""
        if not re.fullmatch(r"[a-z0-9_]{3,24}", end_user_id):
            raise forms.ValidationError("Use 3 to 24 lowercase letters, digits, or underscores.")
        if Profile.objects.exclude(user=self.instance).filter(eds_end_user_id=end_user_id).exists():
            raise forms.ValidationError("This Wuying username is already linked to another user.")
        return end_user_id

    def clean(self):
        cleaned = super(DashboardUserUpdateForm, self).clean()
        password = cleaned.get("password") or ""
        if password or cleaned.get("confirm_password"):
            if password != cleaned.get("confirm_password"):
                self.add_error("confirm_password", "The two dashboard passwords do not match.")
            elif password:
                try:
                    validate_password(password, self.instance)
                except DjangoValidationError as exc:
                    self.add_error("password", exc.messages)
        return cleaned

    def save(self, commit=True):
        user = super(DashboardUserUpdateForm, self).save(commit=False)
        password = self.cleaned_data.get("password") or ""
        if password:
            user.set_password(password)
        if commit:
            user.save()
            end_user_id = self.cleaned_data.get("eds_end_user_id") or None
            profile = Profile.objects.filter(user=user).first()
            if profile:
                profile.eds_end_user_id = end_user_id
                profile.save(update_fields=["eds_end_user_id", "modified"])
            elif end_user_id:
                Profile.objects.create(user=user, eds_end_user_id=end_user_id)
        return user


WUYING_PASSWORD_RULE = "Use at least 10 characters with 3 of: uppercase, lowercase, digits, symbols. No spaces."


def wuying_password_ok(password):
    kinds = sum((
        any(char.isupper() for char in password),
        any(char.islower() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() for char in password),
    ))
    return len(password) >= 10 and kinds >= 3 and ' ' not in password


def generate_wuying_password(length=12):
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%&*-_")
    chars = [secrets.choice(group) for group in groups]
    pool = "".join(groups)
    chars.extend(secrets.choice(pool) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class UserProfileDeleteForm(forms.Form):
    wuying_action = forms.ChoiceField(
        label="Wuying login in Alibaba",
        choices=[
            ("remove", "Also delete the Wuying user in Alibaba"),
            ("keep", "Keep it in Alibaba, only unlink here"),
        ],
        initial="remove",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class WuyingPasswordForm(forms.Form):
    new_password = forms.CharField(
        label="New Wuying password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave both boxes empty and the dashboard makes a 12 character password.",
    )
    confirm_password = forms.CharField(
        label="Type it again",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )

    def clean(self):
        cleaned_data = super(WuyingPasswordForm, self).clean()
        password = cleaned_data.get('new_password') or ''
        if not password and not cleaned_data.get('confirm_password'):
            cleaned_data['new_password'] = generate_wuying_password()
            return cleaned_data
        if password and not wuying_password_ok(password):
            self.add_error('new_password', WUYING_PASSWORD_RULE)
        if password and password != cleaned_data.get('confirm_password'):
            self.add_error('confirm_password', "The two passwords do not match.")
        return cleaned_data