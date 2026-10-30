STATUS_CHOICES = [
    ("Running", "Running"),
    ("Stopped", "Stopped"),
    ("Starting", "Starting"),
    ("Stopping", "Stopping"),
    ("Creating", "Creating"),
    ("Error", "Error"),
]

ACTION_CHOICES = [
    ("Start", "Start"),
    ("Stop", "Stop"),
    ("Restart", "Restart"),
    ("Assign", "Assign"),
    ("Unassign", "Unassign"),
    ("Resize", "Resize"),
    ("Connect", "Connect"),
    ("API Error", "API Error"),
]

# Alibaba desktop types sold pay-as-you-go in Hangzhou: (type ID, group, vCPU, RAM GiB, GPU memory GiB)
DESKTOP_TYPES = [
    ("eds.enterprise_office.2c4g", "Enterprise Office", 2, 4, 0),
    ("eds.enterprise_office.4c4g", "Enterprise Office", 4, 4, 0),
    ("eds.enterprise_office.4c8g", "Enterprise Office", 4, 8, 0),
    ("eds.enterprise_office.4c16g", "Enterprise Office", 4, 16, 0),
    ("eds.enterprise_office.6c12g", "Enterprise Office", 6, 12, 0),
    ("eds.enterprise_office.8c8g", "Enterprise Office", 8, 8, 0),
    ("eds.enterprise_office.8c16g", "Enterprise Office", 8, 16, 0),
    ("eds.enterprise_office.8c32g", "Enterprise Office", 8, 32, 0),
    ("eds.enterprise_office.16c64g", "Enterprise Office", 16, 64, 0),
    ("eds.enterprise_office.32c64g", "Enterprise Office", 32, 64, 0),
    ("eds.enterprise_office.32c128g", "Enterprise Office", 32, 128, 0),
    ("eds.hf.4c8g", "High Frequency Office", 4, 8, 0),
    ("eds.hf.8c16g", "High Frequency Office", 8, 16, 0),
    ("eds.hf.12c24g", "High Frequency Office", 12, 24, 0),
    ("eds.graphics_flagship_pro.8c16g.4g", "Graphics Workstation Pro", 8, 16, 4),
    ("eds.graphics_flagship_pro.16c32g.8g", "Graphics Workstation Pro", 16, 32, 8),
    ("eds.graphics_flagship_pro.16c32g.12g", "Graphics Workstation Pro", 16, 32, 12),
    ("eds.graphics_flagship_pro.32c64g.12g", "Graphics Workstation Pro", 32, 64, 12),
    ("eds.graphics_flagship_pro.32c64g.16g", "Graphics Workstation Pro", 32, 64, 16),
    ("eds.graphics_flagship_pro.32c64g.24g", "Graphics Workstation Pro", 32, 64, 24),
    ("eds.graphics_flagship_pro.64c128g.24g", "Graphics Workstation Pro", 64, 128, 24),
    ("eds.graphics_flagship_elite.32c64g.48g1x", "AI Workstation Elite", 32, 64, 48),
    ("eds.graphics_flagship_elite.64c128g.48g1x", "AI Workstation Elite", 64, 128, 48),
    ("eds.graphics_flagship_elite.64c256g.48g1x", "AI Workstation Elite", 64, 256, 48),
    ("eds.graphics_flagship_elite.64c256g.96g2x", "AI Workstation Elite", 64, 256, 96),
    ("eds.graphics_flagship_elite.96c384g.192g4x", "AI Workstation Elite", 96, 384, 192),
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

SYSTEM_DISK_CHOICES = [
    (80, "80 GB"),
    (120, "120 GB"),
]
SYSTEM_DISK_SIZES = [size for size, _label in SYSTEM_DISK_CHOICES]

DATA_DISK_CHOICES = [
    (0, "No data disk"),
    (100, "100 GB"),
    (200, "200 GB"),
    (500, "500 GB"),
    (1000, "1000 GB"),
]
DATA_DISK_SIZES = [size for size, _label in DATA_DISK_CHOICES]
