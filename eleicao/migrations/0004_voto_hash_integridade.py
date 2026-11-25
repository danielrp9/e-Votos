import hashlib
import json

from django.db import migrations, models


HASH_INICIAL = "0" * 64


def popular_hashes_integridade(apps, schema_editor):
    Voto = apps.get_model("eleicao", "Voto")
    banco = schema_editor.connection.alias
    hashes_anteriores = {}

    votos = Voto.objects.using(banco).order_by("eleicao_id", "pk")
    for voto in votos.iterator():
        hash_anterior = hashes_anteriores.get(voto.eleicao_id, HASH_INICIAL)
        conteudo = {
            "eleicao_id": voto.eleicao_id,
            "chapa_id": voto.chapa_id,
            "tipo_voto": voto.tipo_voto,
            "codigo_comprovante": voto.codigo_comprovante,
            "data_registro": voto.data_registro.isoformat(),
            "hash_anterior": hash_anterior,
        }
        serializado = json.dumps(
            conteudo,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        hash_integridade = hashlib.sha256(serializado).hexdigest()
        Voto.objects.using(banco).filter(pk=voto.pk).update(
            hash_anterior=hash_anterior,
            hash_integridade=hash_integridade,
        )
        hashes_anteriores[voto.eleicao_id] = hash_integridade


class Migration(migrations.Migration):

    dependencies = [
        ("eleicao", "0003_voto_data_registro"),
    ]

    operations = [
        migrations.AddField(
            model_name="voto",
            name="hash_anterior",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="voto",
            name="hash_integridade",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.RunPython(popular_hashes_integridade, migrations.RunPython.noop),
    ]
