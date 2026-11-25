from django.db.models import Count, Q

from eleicao.models import Chapa, Eleitor, Voto


def calcular_resultado_eleicao(eleicao):
    total_votos = Voto.objects.filter(eleicao=eleicao).count()
    total_eleitores_que_votaram = Eleitor.objects.filter(
        eleicao=eleicao,
        ja_votou=True,
    ).count()
    integridade_ok = total_votos == total_eleitores_que_votaram
    resultados = []
    if integridade_ok:
        resultados = (
            Chapa.objects.filter(eleicao=eleicao)
            .annotate(
                total_votos=Count(
                    "votos",
                    filter=Q(votos__tipo_voto=Voto.Tipo.VALIDO),
                )
            )
            .order_by("numero")
        )

    return {
        "total_votos": total_votos,
        "total_eleitores_que_votaram": total_eleitores_que_votaram,
        "integridade_ok": integridade_ok,
        "resultados": resultados,
        "votos_brancos": Voto.objects.filter(
            eleicao=eleicao,
            tipo_voto=Voto.Tipo.BRANCO,
        ).count(),
        "votos_nulos": Voto.objects.filter(
            eleicao=eleicao,
            tipo_voto=Voto.Tipo.NULO,
        ).count(),
    }
