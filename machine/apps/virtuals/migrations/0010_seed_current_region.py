from django.conf import settings
from django.db import migrations


def seed_current_region(apps, schema_editor):
    CloudRegion = apps.get_model("virtuals", "CloudRegion")
    OfficeNetwork = apps.get_model("virtuals", "OfficeNetwork")
    VMProfile = apps.get_model("virtuals", "VMProfile")

    region_id = (getattr(settings, "REGION_ID", "") or "").strip()
    directory_id = (getattr(settings, "DIRECTORY_ID", "") or "").strip()
    if not region_id or region_id == "xxxx":
        return
    policy_id = (getattr(settings, "POLICY_GROUP_ID", "") or "").strip()
    if policy_id == "xxxx":
        policy_id = ""
    region, _created = CloudRegion.objects.get_or_create(
        region_id=region_id,
        defaults={"name": region_id, "enabled": True, "policy_group_id": policy_id},
    )
    if not directory_id or directory_id == "xxxx":
        return
    network, _created = OfficeNetwork.objects.get_or_create(
        directory_id=directory_id,
        defaults={"region": region},
    )
    VMProfile.objects.filter(office_network__isnull=True).update(office_network=network)


class Migration(migrations.Migration):

    dependencies = [
        ("virtuals", "0009_regions_office_networks"),
    ]

    operations = [
        migrations.RunPython(seed_current_region, migrations.RunPython.noop),
    ]
