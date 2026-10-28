from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User

from apps.profiles.models import Profile
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
    directory_username = forms.CharField(
        label="Directory username",
        max_length=64,
        help_text="Username already created in the WorkSpaces directory.",
    )

    class Meta:
        model = VirtualMachine
        fields = ("name", "user", "current_profile")

    def __init__(self, *args, **kwargs):
        super(DesktopCreateForm, self).__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["name"].label = "Desktop name"
        self.fields["name"].help_text = "Windows Server 2022 in Singapore. Billing is AutoStop, hourly while it is running."
        self.fields["user"].required = False
        self.fields["user"].queryset = User.objects.order_by("username")
        self.fields["user"].help_text = "Optional. Assigns this desktop to a dashboard user."
        self.fields["current_profile"].required = False
        self.fields["current_profile"].queryset = VMProfile.objects.order_by("name")
        self.fields["current_profile"].help_text = "Leave empty for the Standard Windows bundle, 2 vCPU and 4 GB."

    def clean_directory_username(self):
        username = self.cleaned_data["directory_username"].strip()
        if not username or not username[0].isalpha():
            raise forms.ValidationError("Start the directory username with a letter.")
        return username

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
    class Meta:
        model = VMProfile
        exclude = ('created_by', 'modified_by', 'created', 'modified')


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