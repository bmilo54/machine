from django.contrib.auth.models import User
from django.db import models

from apps.virtuals.choices import ACTION_CHOICES, STATUS_CHOICES
from core.abstract_models import TimeStampedModel


class VMProfile(TimeStampedModel):
    """
    Hardware Configure 机器配置模板 (例如: "基础办公型", "重度开发型")
    """
    name = models.CharField(verbose_name="Settings Name", max_length=255, blank=True, null=True, unique=True)
    cpu = models.IntegerField(verbose_name="CPU Cores", blank=True, null=True)
    ram = models.IntegerField(verbose_name="RAM (GB)", blank=True, null=True)
    system_disk = models.IntegerField(verbose_name="System Disk (GB)", blank=True, null=True)
    data_disk = models.IntegerField(verbose_name="Data Disk (GB)", blank=True, null=True)

    # 阿里云无影对应的实际规格代码，非常重要
    # 例如: eds.enterprise_office.2c8g
    instance_type_code = models.CharField(verbose_name="Instance Type Code", max_length=255, blank=True, null=True, help_text="Alibaba ECD Actual Codes")

    class Meta:
        verbose_name = "VM Profile"
        verbose_name_plural = "VM Profiles"

    def __str__(self):
        return self.name or "VM Profile"


class VirtualMachine(TimeStampedModel):
    """
    核心表：对应阿里云上的无影云桌面实例。
    直接与 User 表建立外键关系，一台桌面同一时间只归属一个用户。
    """
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        verbose_name="User",
        blank=True,
        null=True,
        related_name="virtual_machines",
    )
    name = models.CharField(verbose_name="Desktop Name", max_length=255, blank=True, null=True)
    vm_id = models.CharField(verbose_name="VM ID", max_length=200, blank=True, null=True, unique=True, help_text="Alibaba ECD Instance ID (ecd-xxxxxxx)")
    directory_id = models.CharField(max_length=100, help_text="Directory ID", blank=True)
    region = models.CharField(max_length=50, default="cn-hangzhou")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Creating")

    current_profile = models.ForeignKey(VMProfile, on_delete=models.SET_NULL, null=True, blank=True)
    cpu = models.IntegerField(help_text="Actual CPU Cores", blank=True, null=True)
    ram = models.IntegerField(help_text="Actual RAM (GB)", blank=True, null=True)
    system_disk = models.IntegerField(default=80)
    data_disk = models.IntegerField(default=0)

    # 网络信息
    private_ip = models.GenericIPAddressField(null=True, blank=True)
    public_ip = models.GenericIPAddressField(null=True, blank=True)

    last_start_time = models.DateTimeField(null=True, blank=True)
    last_stop_time = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.vm_id})"

    class Meta:
        verbose_name = "Virtual Machine"
        verbose_name_plural = "Virtual Machines"


class ActivityLog(TimeStampedModel):
    """
    操作审计日志表
    记录管理员或系统对虚拟机的所有操作。
    用户被删除后保留日志，只把操作者置空。
    """
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        verbose_name="Actor",
        blank=True,
        null=True,
        related_name="performed_actions",
    )
    virtual_machine = models.ForeignKey(
        VirtualMachine,
        on_delete=models.CASCADE,
        verbose_name="Virtual Machine",
        related_name="activity_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, blank=True, null=True)

    # 记录详细变更
    old_value = models.TextField(blank=True, help_text="修改前的值(如配置升降配)")
    new_value = models.TextField(blank=True, help_text="修改后的值")
    details = models.TextField(blank=True, help_text="如API报错信息")

    ip_address = models.GenericIPAddressField(null=True, blank=True, help_text="操作来源IP")

    def __str__(self):
        return f"{self.actor} {self.action} {self.virtual_machine} at {self.created}"
