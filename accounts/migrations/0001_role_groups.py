"""Create the three analyst-workflow roles as auth Groups.

Kept as a migration rather than manual admin clicks so every environment ends
up with the same roles. Permissions are attached in a later phase, once the
per-app views that need guarding exist.
"""
from django.db import migrations

ROLES = ['Analyst', 'Reviewer', 'Admin']


def create_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    for name in ROLES:
        Group.objects.get_or_create(name=name)


def delete_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=ROLES).delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(create_groups, delete_groups),
    ]
