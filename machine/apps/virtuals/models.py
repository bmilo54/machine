from django.contrib.auth.models import User
from django.db import models

from apps.virtuals.choices import ACTION_CHOICES, DESKTOP_TYPE_SPECS, STATUS_CHOICES, gpu_card_name
from core.abstract_models import TimeStampedModel


class CloudRegion(TimeStampedModel):
    region_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100, blank=True)
    enabled = models.BooleanField(default=False)
    policy_group_id = models.CharField(max_length=100, blank=True)
    policy_group_name = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Region"
        verbose_name_plural = "Regions"
        ordering = ["name", "region_id"]

    def __str__(self):
        return self.name or self.region_id


class OfficeNetwork(TimeStampedModel):
    region = models.ForeignKey(CloudRegion, on_delete=models.CASCADE, related_name="office_networks")
    directory_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=30, blank=True)
    desktop_types = models.JSONField(default=list, blank=True)
    types_synced = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Office network"
        verbose_name_plural = "Office networks"
        ordering = ["region__name", "name", "directory_id"]

    def __str__(self):
        return "{} · {}".format(self.region, self.display_name)

    @property
    def display_name(self):
        return self.name or self.directory_id.split("+", 1)[-1]


class VMProfile(TimeStampedModel):
    """
    Hardware Configure 机器配置模板 (例如: "基础办公型", "重度开发型")
    """
    name = models.CharField(verbose_name="Settings Name", max_length=255, blank=True, null=True, unique=True)
    cpu = models.IntegerField(verbose_name="CPU Cores", blank=True, null=True)
    ram = models.IntegerField(verbose_name="RAM (GB)", blank=True, null=True)
    gpu = models.IntegerField(verbose_name="GPU Memory (GB)", blank=True, null=True)
    system_disk = models.IntegerField(verbose_name="System Disk (GB)", blank=True, null=True)
    data_disk = models.IntegerField(verbose_name="Data Disk (GB)", blank=True, null=True)
    bandwidth_mode = models.CharField(max_length=16, default="basic")
    bandwidth_mbps = models.PositiveIntegerField(default=10)
    internet_charge_type = models.CharField(max_length=20, default="PayByTraffic")

    # 阿里云无影对应的实际规格代码，非常重要
    # 例如: eds.general.4c8g
    instance_type_code = models.CharField(verbose_name="Instance Type Code", max_length=255, blank=True, null=True, help_text="Alibaba ECD Actual Codes")
    office_network = models.ForeignKey(
        OfficeNetwork,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="profiles",
    )

    class Meta:
        verbose_name = "VM Profile"
        verbose_name_plural = "VM Profiles"

    def __str__(self):
        return self.name or "VM Profile"

    @property
    def desktop_type_group(self):
        spec = DESKTOP_TYPE_SPECS.get(self.instance_type_code or "")
        return spec["group"] if spec else ""

    @property
    def gpu_card_name(self):
        return gpu_card_name(self.instance_type_code)

    @property
    def bandwidth_label(self):
        if self.bandwidth_mode != "premium":
            return "No premium"
        charge = "by bandwidth" if self.internet_charge_type == "PayByBandwidth" else "by data transfer"
        return "Premium {} Mbps, {}".format(self.bandwidth_mbps or 0, charge)


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
    desktop_type = models.CharField(max_length=128, blank=True, help_text="Alibaba desktop type, such as eds.enterprise_office.6c12g")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Creating")
    status_detail = models.CharField(max_length=255, blank=True)

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


class ScreenSummary(TimeStampedModel):
    """What was on screen during one recording. Kept after Alibaba deletes the video."""

    file_path = models.CharField(max_length=1000, unique=True)
    desktop_id = models.CharField(max_length=200, blank=True)
    desktop_name = models.CharField(max_length=255, blank=True)
    end_user_id = models.CharField(max_length=200, blank=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="done")
    detail = models.TextField(blank=True)
    lines = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started", "-created"]

    def __str__(self):
        return self.desktop_name or self.file_path


class WorkReport(TimeStampedModel):
    """A written account of one person's screen for one day."""

    end_user_id = models.CharField(max_length=200)
    day = models.DateField()
    report = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["end_user_id", "day"], name="unique_work_report_day"),
        ]
        ordering = ["-day", "end_user_id"]

    def __str__(self):
        return "{} {}".format(self.end_user_id, self.day)


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
        on_delete=models.SET_NULL,
        verbose_name="Virtual Machine",
        related_name="activity_logs",
        blank=True,
        null=True,
    )
    desktop_name = models.CharField(max_length=255, blank=True)
    cloud_id = models.CharField(max_length=200, blank=True)
    region = models.CharField(max_length=50, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, blank=True, null=True)

    # 记录详细变更
    old_value = models.TextField(blank=True, help_text="修改前的值(如配置升降配)")
    new_value = models.TextField(blank=True, help_text="修改后的值")
    details = models.TextField(blank=True, help_text="如API报错信息")

    ip_address = models.GenericIPAddressField(null=True, blank=True, help_text="操作来源IP")

    def __str__(self):
        target = self.virtual_machine or self.desktop_name or "desktop"
        return f"{self.actor} {self.action} {target} at {self.created}"

    @property
    def subject(self):
        if self.virtual_machine_id and self.virtual_machine.name:
            return self.virtual_machine.name
        return self.desktop_name or "Deleted desktop"
