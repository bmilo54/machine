from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('virtuals', '0007_desktop_status_detail'),
    ]

    operations = [
        migrations.AddField(
            model_name='vmprofile',
            name='bandwidth_mode',
            field=models.CharField(default='basic', max_length=16),
        ),
        migrations.AddField(
            model_name='vmprofile',
            name='bandwidth_mbps',
            field=models.PositiveIntegerField(default=10),
        ),
        migrations.AddField(
            model_name='vmprofile',
            name='internet_charge_type',
            field=models.CharField(default='PayByTraffic', max_length=20),
        ),
    ]
