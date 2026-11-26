import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("eleicao", "0004_voto_hash_integridade"),
    ]

    operations = [
        migrations.AddField(
            model_name="eleicao",
            name="criada_em",
            field=models.DateTimeField(editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="eleicao",
            name="criada_em",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                editable=False,
                null=True,
            ),
        ),
    ]
