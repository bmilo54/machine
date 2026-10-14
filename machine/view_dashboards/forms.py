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