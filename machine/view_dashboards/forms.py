import re
import secrets
import string

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User

from apps.profiles.models import Profile
from apps.virtuals.choices import (
    DATA_DISK_CHOICES,
    DESKTOP_TYPE_CHOICES,
    DESKTOP_TYPE_SPECS,
    SYSTEM_DISK_CHOICES,
)
from apps.virtuals.models import VMProfile, VirtualMachine

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
        self.fields["user"].queryset = User.objects.order_by("username")
        self.fields["user"].help_text = "Optional. The desktop is assigned to this user in the dashboard."
        self.fields["current_profile"].required = False
        self.fields["current_profile"].queryset = VMProfile.objects.order_by("name")
        self.fields["current_profile"].help_text = "Leave empty for 6 vCPU, 12 GB, an 80 GB system disk, and no data disk."

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if len(name) > 64:
            raise forms.ValidationError("Use 64 characters or fewer.")
        if name.lower().startswith(("http://", "https://")) or not name[0].isalpha():
            raise forms.ValidationError("Start the name with a letter.")
        return name


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
        self.fields['user'].queryset = User.objects.order_by('username')
        self.fields['current_profile'].queryset = VMProfile.objects.order_by('name')


class VMProfileForm(forms.ModelForm):
    instance_type_code = forms.ChoiceField(
        label="Desktop type",
        choices=DESKTOP_TYPE_CHOICES,
        initial="eds.enterprise_office.6c12g",
        help_text="Same hardware list as the Alibaba console. CPU, RAM, and GPU fill in from this type.",
    )
    system_disk = forms.TypedChoiceField(
        label="System Disk (GB)",
        choices=SYSTEM_DISK_CHOICES,
        coerce=int,
        initial=80,
        help_text="Sizes offered by Alibaba for pay-as-you-go desktops.",
    )
    data_disk = forms.TypedChoiceField(
        label="Data Disk (GB)",
        choices=DATA_DISK_CHOICES,
        coerce=int,
        empty_value=0,
        required=False,
        help_text="Sizes offered by Alibaba for pay-as-you-go desktops.",
    )

    class Meta:
        model = VMProfile
        fields = ('name', 'instance_type_code', 'system_disk', 'data_disk')

    def save(self, commit=True):
        profile = super(VMProfileForm, self).save(commit=False)
        spec = DESKTOP_TYPE_SPECS[profile.instance_type_code]
        profile.cpu = spec["cpu"]
        profile.ram = spec["ram"]
        profile.gpu = spec["gpu"]
        if commit:
            profile.save()
        return profile


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ('user', 'eds_end_user_id')

    def __init__(self, *args, **kwargs):
        super(UserProfileForm, self).__init__(*args, **kwargs)
        users = User.objects.order_by('username')
        if self.instance.pk:
            self.fields['user'].disabled = True
        else:
            users = users.filter(profile__isnull=True)
        self.fields['user'].queryset = users


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


WUYING_MODE_CHOICES = [
    ("create", "Create a new Wuying user in Alibaba"),
    ("link", "Link a Wuying user that already exists"),
]


class UserProfileCreateForm(UserProfileForm):
    wuying_mode = forms.ChoiceField(
        label="Wuying account",
        choices=WUYING_MODE_CHOICES,
        initial="create",
    )
    email = forms.EmailField(
        label="Email",
        required=False,
        help_text="Alibaba sends desktop notices here. Leave empty to use the dashboard user's email.",
    )
    password = forms.CharField(
        label="Wuying password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave empty and the dashboard makes a 12 character password. It is shown on the Users page.",
    )
    display_name = forms.CharField(label="Display name", required=False, max_length=64)

    field_order = ('user', 'wuying_mode', 'eds_end_user_id', 'email', 'password', 'display_name')

    def __init__(self, *args, **kwargs):
        super(UserProfileCreateForm, self).__init__(*args, **kwargs)
        self.fields['eds_end_user_id'].required = True
        self.fields['eds_end_user_id'].label = "Wuying username"
        self.fields['eds_end_user_id'].help_text = "3 to 24 lowercase letters, digits, or underscores. The user signs in to Wuying with this."

    def clean_eds_end_user_id(self):
        end_user_id = (self.cleaned_data.get('eds_end_user_id') or '').strip()
        if not re.fullmatch(r'[a-z0-9_]{3,24}', end_user_id):
            raise forms.ValidationError("Use 3 to 24 lowercase letters, digits, or underscores.")
        return end_user_id

    def clean(self):
        cleaned_data = super(UserProfileCreateForm, self).clean()
        if cleaned_data.get('wuying_mode') != 'create':
            return cleaned_data

        user = cleaned_data.get('user')
        if not cleaned_data.get('email') and user and user.email:
            cleaned_data['email'] = user.email
        if not cleaned_data.get('email'):
            self.add_error('email', "Alibaba needs an email for a new Wuying user.")

        if not cleaned_data.get('password'):
            cleaned_data['password'] = generate_wuying_password()
        elif not wuying_password_ok(cleaned_data['password']):
            self.add_error('password', WUYING_PASSWORD_RULE)
        return cleaned_data


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