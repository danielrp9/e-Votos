import hashlib
import json

from django.db.models import Count, Q

from eleicao.models import Chapa, Eleitor, Voto
from eleicao.services.resultado_eleicao import calcular_resultado_eleicao


def obter_dados_boletim(eleicao):
    resultado = calcular_resultado_eleicao(eleicao)
    total_eleitores_aptos = Eleitor.objects.filter(eleicao=eleicao).count()
    comparecimento = resultado["total_eleitores_que_votaram"]
    abstencoes = total_eleitores_aptos - comparecimento
    total_votos_validos = Voto.objects.filter(
        eleicao=eleicao,
        tipo_voto=Voto.Tipo.VALIDO,
    ).count()
    votos_por_chapa = list(
        Chapa.objects.filter(eleicao=eleicao)
        .annotate(
            total_votos=Count(
                "votos",
                filter=Q(votos__tipo_voto=Voto.Tipo.VALIDO),
            )
        )
        .order_by("numero")
        .values_list("numero", "total_votos")
    )
    comprovantes_ordenados = list(
        Voto.objects.filter(eleicao=eleicao)
        .order_by("codigo_comprovante")
        .values_list("codigo_comprovante", flat=True)
    )
    dados_hash = {
        "eleicao_id": eleicao.pk,
        "votos_por_chapa": [
            {"numero": numero, "total_votos": total}
            for numero, total in votos_por_chapa
        ],
        "comprovantes": comprovantes_ordenados,
    }
    conteudo_hash = json.dumps(
        dados_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    percentual_comparecimento = (
        comparecimento * 100 / total_eleitores_aptos if total_eleitores_aptos else 0
    )
    percentual_abstencoes = (
        abstencoes * 100 / total_eleitores_aptos if total_eleitores_aptos else 0
    )

    resultados = list(resultado["resultados"])
    maior_total = max((chapa.total_votos for chapa in resultados), default=0)
    vencedoras = [
        chapa
        for chapa in resultados
        if chapa.total_votos == maior_total and maior_total > 0
    ]
    vencedor_id = vencedoras[0].pk if len(vencedoras) == 1 else None
    for chapa in resultados:
        chapa.percentual = (
            chapa.total_votos * 100 / total_votos_validos
            if total_votos_validos
            else 0
        )
        chapa.vencedora = chapa.pk == vencedor_id

    return {
        **resultado,
        "resultados": resultados,
        "total_eleitores_aptos": total_eleitores_aptos,
        "comparecimento": comparecimento,
        "percentual_comparecimento": percentual_comparecimento,
        "abstencoes": abstencoes,
        "percentual_abstencoes": percentual_abstencoes,
        "total_votos_validos": total_votos_validos,
        "votos_por_chapa": votos_por_chapa,
        "comprovantes_ordenados": comprovantes_ordenados,
        "assinatura_digital": hashlib.sha256(conteudo_hash).hexdigest(),
    }
