STATUS_CHOICES = [
    ("Running", "Running"),
    ("Stopped", "Stopped"),
    ("Starting", "Starting"),
    ("Stopping", "Stopping"),
    ("Creating", "Creating"),
    ("Updating", "Updating"),
    ("Upgrading", "Upgrading"),
    ("Error", "Error"),
]

ACTION_CHOICES = [
    ("Create", "Create"),
    ("Start", "Start"),
    ("Stop", "Stop"),
    ("Restart", "Restart"),
    ("Assign", "Assign"),
    ("Unassign", "Unassign"),
    ("Update", "Update"),
    ("Resize", "Change specifications"),
    ("Connect", "Connect"),
    ("Delete", "Delete"),
    ("Sync", "Sync"),
    ("Create user", "Create user"),
    ("Delete user", "Delete user"),
    ("Set password", "Set password"),
    ("API Error", "API Error"),
]

# Alibaba desktop types sold pay-as-you-go in Singapore: (type ID, group, vCPU, RAM GiB, GPU memory GiB)
DESKTOP_TYPES = [
    ("eds.general.2c2g", "General Office", 2, 2, 0),
    ("eds.general.2c4g", "General Office", 2, 4, 0),
    ("eds.general.2c8g", "General Office", 2, 8, 0),
    ("eds.general.4c8g", "General Office", 4, 8, 0),
    ("eds.general.4c16g", "General Office", 4, 16, 0),
    ("eds.general.8c16g", "General Office", 8, 16, 0),
    ("eds.general.8c32g", "General Office", 8, 32, 0),
    ("eds.general.16c32g", "General Office", 16, 32, 0),
    ("eds.general.16c64g", "General Office", 16, 64, 0),
    ("eds.general.32c64g", "General Office", 32, 64, 0),
    ("eds.general.32c128g", "General Office", 32, 128, 0),
    ("eds.graphics_flagship_pro.8c16g.4g", "Cloud Computer Graphics", 8, 16, 4),
    ("eds.graphics_flagship_pro.16c32g.8g", "Graphics Workstation (Ultimate)", 16, 32, 8),
    ("eds.graphics_flagship_pro.16c32g.12g", "Graphics Workstation (Ultimate)", 16, 32, 12),
    ("eds.graphics_flagship_pro.32c64g.12g", "Graphics Workstation (Ultimate)", 32, 64, 12),
    ("eds.graphics_flagship_pro.32c64g.16g", "Graphics Workstation (Ultimate)", 32, 64, 16),
    ("eds.graphics_flagship_pro.32c64g.24g", "Graphics Workstation (Ultimate)", 32, 64, 24),
    ("eds.graphics_flagship_pro.64c128g.24g", "Graphics Workstation (Ultimate)", 64, 128, 24),
    ("eds.graphics_flagship_elite.32c64g.48g1x", "Professional Workstation", 32, 64, 48),
    ("eds.graphics_flagship_elite.64c128g.48g1x", "Professional Workstation", 64, 128, 48),
    ("eds.graphics_flagship_elite.64c256g.48g1x", "Professional Workstation", 64, 256, 48),
    ("eds.graphics.4c10g.2gt4", "Graphics Workstation (Basic)", 4, 10, 2),
    ("eds.graphics.8c16g.4gt4", "Graphics Workstation (Basic)", 8, 16, 4),
    ("eds.graphics.8c31g.16gt4", "Graphics Workstation (Basic)", 8, 31, 16),
    ("eds.graphics.16c32g.8gt4", "Graphics Workstation (Basic)", 16, 32, 8),
    ("eds.graphics.16c1t4", "Graphics Workstation (Basic)", 16, 62, 16),
]
DESKTOP_TYPE_SPECS = {
    type_id: {"group": group, "cpu": cpu, "ram": ram, "gpu": gpu}
    for type_id, group, cpu, ram, gpu in DESKTOP_TYPES
}


def desktop_type_label(type_id):
    spec = DESKTOP_TYPE_SPECS.get(type_id)
    if not spec:
        return type_id or ""
    label = "{} vCPU, {} GiB".format(spec["cpu"], spec["ram"])
    if spec["gpu"]:
        label += ", {} GiB GPU".format(spec["gpu"])
    return label


DESKTOP_TYPE_CHOICES = []
for _group in dict.fromkeys(group for _id, group, _cpu, _ram, _gpu in DESKTOP_TYPES):
    DESKTOP_TYPE_CHOICES.append((
        _group,
        [(type_id, desktop_type_label(type_id)) for type_id, group, *_rest in DESKTOP_TYPES if group == _group],
    ))

DISK_STEP = 10
SYSTEM_DISK_MIN = 60
SYSTEM_DISK_MAX = 2040
DATA_DISK_MIN = 50
DATA_DISK_MAX = 2040


def gpu_card_name(type_id):
    type_id = type_id or ""
    if type_id.startswith("eds.graphics_flagship"):
        return "NVIDIA RTX 5880"
    if type_id.startswith("eds.graphics"):
        return "NVIDIA T4"
    return ""


def desktop_type_family(type_id):
    type_id = type_id or ""
    if type_id.startswith("eds.graphics_flagship_elite"):
        return "graphics_elite"
    if type_id.startswith("eds.graphics_flagship_pro"):
        return "graphics_pro"
    if type_id.startswith("eds.graphics"):
        return "graphics"
    if type_id.startswith("eds.general"):
        return "office"
    return ""


def family_type_choices(type_id):
    family = desktop_type_family(type_id)
    matched = [row for row in DESKTOP_TYPES if desktop_type_family(row[0]) == family]
    choices = []
    for group in dict.fromkeys(row[1] for row in matched):
        choices.append((
            group,
            [(row_id, desktop_type_label(row_id)) for row_id, row_group, *_rest in matched if row_group == group],
        ))
    return choices


def spec_summary(type_id, system_disk, data_disk):
    hardware = desktop_type_label(type_id) if type_id in DESKTOP_TYPE_SPECS else (type_id or "unknown type")
    return "{} · system {} GiB · data {} GiB".format(hardware, system_disk, data_disk)


# ModifyDesktopSpec limits. Disks can only grow, in steps of 10 GiB.
SPEC_SYSTEM_MIN = 80
SPEC_SYSTEM_MAX = 500
SPEC_DATA_MAX = 1020
SPEC_DATA_MIN = {"office": 20, "graphics": 40, "graphics_pro": 40, "graphics_elite": 40}
SPEC_CHANGE_WAIT_MINUTES = 5


def valid_grown_disk(size, current, floor, ceiling):
    try:
        size = int(size)
        current = int(current or 0)
    except (TypeError, ValueError):
        return False
    if size == current:
        return True
    if size < current or size < floor or size > ceiling or size % DISK_STEP != 0:
        return False
    return True


def valid_disk_size(size, minimum, maximum, allow_zero=False):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return False
    if allow_zero and size == 0:
        return True
    return minimum <= size <= maximum and size % DISK_STEP == 0
