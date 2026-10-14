from django.contrib import admin
from .models import VMProfile, VirtualMachine, ActivityLog

@admin.register(VMProfile)
class VMProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'cpu', 'ram', 'system_disk', 'data_disk', 'instance_type_code')
    search_fields = ('name', 'instance_type_code')

@admin.register(VirtualMachine)
class VirtualMachineAdmin(admin.ModelAdmin):
    list_display = ('user', 'name', 'vm_id', 'directory_id', 'region', 'status', 'last_start_time', 'last_stop_time')
    search_fields = ('user__username', 'name', 'vm_id')

@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('actor', 'virtual_machine', 'action', 'created')
    search_fields = ('actor__username', 'virtual_machine__name', 'action')
