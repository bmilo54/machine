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

# Pay-as-you-go desktop types across the regions this account can use.
# (type ID, group, vCPU, RAM GiB, GPU memory GiB). GPU memory 0 means no GPU.
# A region only shows the types its office network sells.
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
    ("eds.enterprise_office.2c4g", "Enterprise Office", 2, 4, 0),
    ("eds.enterprise_office.4c4g", "Enterprise Office", 4, 4, 0),
    ("eds.enterprise_office.4c8g", "Enterprise Office", 4, 8, 0),
    ("eds.enterprise_office.4c16g", "Enterprise Office", 4, 16, 0),
    ("eds.enterprise_office.6c12g", "Enterprise Office", 6, 12, 0),
    ("eds.enterprise_office.8c8g", "Enterprise Office", 8, 8, 0),
    ("eds.enterprise_office.8c16g", "Enterprise Office", 8, 16, 0),
    ("eds.enterprise_office.8c32g", "Enterprise Office", 8, 32, 0),
    ("eds.enterprise_office.16c32g", "Enterprise Office", 16, 32, 0),
    ("eds.enterprise_office.16c64g", "Enterprise Office", 16, 64, 0),
    ("eds.enterprise_office.32c64g", "Enterprise Office", 32, 64, 0),
    ("eds.enterprise_office.32c128g", "Enterprise Office", 32, 128, 0),
    ("eds.hf.4c8g", "High Frequency Office", 4, 8, 0),
    ("eds.hf.8c16g", "High Frequency Office", 8, 16, 0),
    ("eds.hf.12c24g", "High Frequency Office", 12, 24, 0),
    ("eds.graphics_flagship_pro.8c16g.4g", "Cloud Computer Graphics", 8, 16, 4),
    ("eds.graphics_flagship_pro.4c8g.2g", "Graphics Workstation (Ultimate)", 4, 8, 2),
    ("eds.graphics_flagship_pro.6c12g.2g", "Graphics Workstation (Ultimate)", 6, 12, 2),
    ("eds.graphics_flagship_pro.8c16g.2g", "Graphics Workstation (Ultimate)", 8, 16, 2),
    ("eds.graphics_flagship_pro.16c32g.8g", "Graphics Workstation (Ultimate)", 16, 32, 8),
    ("eds.graphics_flagship_pro.16c32g.12g", "Graphics Workstation (Ultimate)", 16, 32, 12),
    ("eds.graphics_flagship_pro.32c64g.12g", "Graphics Workstation (Ultimate)", 32, 64, 12),
    ("eds.graphics_flagship_pro.32c64g.16g", "Graphics Workstation (Ultimate)", 32, 64, 16),
    ("eds.graphics_flagship_pro.32c64g.24g", "Graphics Workstation (Ultimate)", 32, 64, 24),
    ("eds.graphics_flagship_pro.64c128g.24g", "Graphics Workstation (Ultimate)", 64, 128, 24),
    ("eds.graphics_flagship_elite.32c64g.48g1x", "Professional Workstation", 32, 64, 48),
    ("eds.graphics_flagship_elite.64c128g.48g1x", "Professional Workstation", 64, 128, 48),
    ("eds.graphics_flagship_elite.64c256g.48g1x", "Professional Workstation", 64, 256, 48),
    ("eds.graphics_flagship_elite.64c256g.96g2x", "Professional Workstation", 64, 256, 96),
    ("eds.graphics_flagship_elite.96c384g.192g4x", "Professional Workstation", 96, 384, 192),
    ("eds.enterprise_graph.4c16g.2ga10", "Enterprise Graphics", 4, 16, 2),
    ("eds.enterprise_graph.8c32g.4ga10", "Enterprise Graphics", 8, 32, 4),
    ("eds.enterprise_graph.16c62g.8ga10", "Enterprise Graphics", 16, 62, 8),
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
    if type_id.startswith("eds.enterprise_graph"):
        return "NVIDIA A10"
    if type_id.startswith("eds.graphics_flagship"):
        return "NVIDIA RTX 5880"
    if type_id.startswith("eds.graphics"):
        return "NVIDIA T4"
    return ""


def desktop_type_family(type_id):
    type_id = type_id or ""
    if type_id.startswith("eds.enterprise_office"):
        return "enterprise_office"
    if type_id.startswith("eds.enterprise_graph"):
        return "enterprise_graph"
    if type_id.startswith("eds.hf."):
        return "high_frequency"
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
SPEC_DATA_MIN = {
    "office": 20,
    "enterprise_office": 20,
    "high_frequency": 20,
    "graphics": 40,
    "graphics_pro": 40,
    "graphics_elite": 40,
    "enterprise_graph": 40,
}
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
