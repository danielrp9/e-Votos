import subprocess
import uuid

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .models import Chapa, Eleicao, Eleitor, Voto
from .services.auditoria_eleicao import (
    obter_dados_boletim,
    verificar_integridade_votos,
)
from .services.resultado_eleicao import calcular_resultado_eleicao


MAX_TENTATIVAS_MATRICULA = 5
REPOSITORIO_PUBLICO_URL = "https://github.com/danielrp9/e-Votos"


def _construir_timeline_eleicao(eleicao):
    votos = list(
        Voto.objects.filter(eleicao=eleicao).order_by("pk", "data_registro")
    )
    eventos = [
        {
            "tipo": "criacao",
            "titulo": "Criação da eleição",
            "data_hora": eleicao.criada_em,
            "referencia": f"Pleito #{eleicao.pk}",
            "mostrar_por_padrao": True,
        },
        {
            "tipo": "abertura",
            "titulo": "Abertura da votação · Zerésima",
            "data_hora": eleicao.data_inicio,
            "referencia": f"Pleito #{eleicao.pk}",
            "hash_atual": Voto.HASH_INICIAL,
            "mostrar_por_padrao": True,
        },
    ]

    hash_anterior_esperado = Voto.HASH_INICIAL
    total_votos = len(votos)
    for indice, voto in enumerate(votos, start=1):
        hash_esperado = voto.calcular_hash_integridade(hash_anterior_esperado)
        elo_integro = (
            voto.hash_anterior == hash_anterior_esperado
            and voto.hash_integridade == hash_esperado
        )
        eventos.append(
            {
                "tipo": "voto",
                "titulo": f"Cédula #{indice:03d}",
                "data_hora": voto.data_registro,
                "hash_anterior": voto.hash_anterior[:8],
                "hash_atual": voto.hash_integridade[:8],
                "elo_integro": elo_integro,
                "mostrar_por_padrao": (
                    total_votos <= 20 or indice <= 3 or indice > total_votos - 5
                ),
            }
        )
        hash_anterior_esperado = hash_esperado

    if eleicao.status_atual == "ENCERRADA":
        total_votantes = Eleitor.objects.filter(
            eleicao=eleicao,
            ja_votou=True,
        ).count()
        eventos.append(
            {
                "tipo": "encerramento",
                "titulo": "Encerramento da urna",
                "data_hora": eleicao.data_fim,
                "total_votantes": total_votantes,
                "total_cedulas": total_votos,
                "paridade_confirmada": total_votantes == total_votos,
                "hash_final": (
                    votos[-1].hash_integridade[:8]
                    if votos
                    else Voto.HASH_INICIAL[:8]
                ),
                "mostrar_por_padrao": True,
            }
        )

    eventos.sort(
        key=lambda evento: (
            evento["data_hora"] is None,
            evento["data_hora"],
        )
    )
    return eventos, any(
        evento["tipo"] == "voto" and not evento["mostrar_por_padrao"]
        for evento in eventos
    )


def _obter_identificacao_versao():
    try:
        resultado = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=settings.BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        commit_hash = resultado.stdout.strip() or "indisponível"
    except (OSError, subprocess.SubprocessError):
        commit_hash = "indisponível"
    commit_url = (
        f"{REPOSITORIO_PUBLICO_URL}/commit/{commit_hash}"
        if commit_hash != "indisponível"
        else REPOSITORIO_PUBLICO_URL
    )
    return {
        "commit_hash": commit_hash,
        "commit_url": commit_url,
        "repositorio_url": REPOSITORIO_PUBLICO_URL,
    }


def _matricula_normalizada(valor):
    return valor.strip().lstrip("0")


def _nome_mascarado(nome):
    partes = nome.split()
    if len(partes) < 2:
        return nome
    return f"{partes[0]} " + " ".join(f"{parte[0]}." for parte in partes[1:])


def _render_resultado_publico(request, eleicao):
    return render(
        request,
        "eleicao/resultado_eleicao.html",
        {
            "eleicao": eleicao,
            **calcular_resultado_eleicao(eleicao),
        },
    )


def _render_se_eleicao_indisponivel(request, eleicao):
    status = eleicao.status_atual
    if status == "ENCERRADA":
        request.session.pop("token_votacao", None)
        return _render_resultado_publico(request, eleicao)
    if status == "AGENDADA":
        return render(request, "eleicao/eleicao_agendada.html", {"eleicao": eleicao})
    return None


@require_GET
def votar(request, token):
    get_object_or_404(Eleitor, token_convite=token)
    request.session["token_votacao"] = str(token)
    return redirect("eleicao:cabina")


@require_http_methods(["GET", "POST"])
def cabina(request):
    token = request.session.get("token_votacao")
    try:
        token = uuid.UUID(str(token))
    except (AttributeError, TypeError, ValueError):
        request.session.pop("token_votacao", None)
        return redirect("eleicao:inicio")

    eleitor = get_object_or_404(
        Eleitor.objects.select_related("eleicao"),
        token_convite=token,
    )
    resposta = _render_se_eleicao_indisponivel(request, eleitor.eleicao)
    if resposta is not None:
        return resposta

    chave_tentativas = f"tentativas_matricula_{token}"
    if request.session.get(chave_tentativas, 0) >= MAX_TENTATIVAS_MATRICULA:
        return render(request, "eleicao/acesso_bloqueado.html", status=429)

    if request.method == "POST":
        matricula = _matricula_normalizada(request.POST.get("matricula", ""))
        if matricula != _matricula_normalizada(eleitor.matricula):
            tentativas = request.session.get(chave_tentativas, 0) + 1
            request.session[chave_tentativas] = tentativas
            if tentativas >= MAX_TENTATIVAS_MATRICULA:
                return render(request, "eleicao/acesso_bloqueado.html", status=429)
            messages.error(request, "Matrícula incorreta. Confira os dados e tente novamente.")
        else:
            if eleitor.ja_votou:
                request.session.pop(chave_tentativas, None)
                request.session.pop("token_votacao", None)
                return render(
                    request,
                    "eleicao/voto_ja_registrado.html",
                    {"eleicao": eleitor.eleicao},
                )
            if eleitor.eleicao.status_atual != "ABERTA":
                resposta = _render_se_eleicao_indisponivel(request, eleitor.eleicao)
                if resposta is not None:
                    return resposta
            request.session.cycle_key()
            request.session["eleitor_id"] = eleitor.pk
            request.session["eleicao_id"] = eleitor.eleicao_id
            request.session.pop(chave_tentativas, None)
            return redirect("eleicao:urna")

    return render(
        request,
        "eleicao/validar_eleitor.html",
        {"eleitor": eleitor, "nome_mascarado": _nome_mascarado(eleitor.nome)},
    )


@require_http_methods(["GET", "POST"])
def urna(request):
    eleitor_id = request.session.get("eleitor_id")
    eleicao_id = request.session.get("eleicao_id")
    if not eleitor_id or not eleicao_id:
        return redirect("eleicao:inicio")

    eleitor = get_object_or_404(Eleitor.objects.select_related("eleicao"), pk=eleitor_id)
    if eleitor.eleicao_id != eleicao_id:
        request.session.pop("eleitor_id", None)
        request.session.pop("eleicao_id", None)
        return redirect("eleicao:inicio")
    if eleitor.eleicao.status_atual == "ENCERRADA":
        request.session.pop("eleitor_id", None)
        request.session.pop("eleicao_id", None)
        return _render_resultado_publico(request, eleitor.eleicao)
    if eleitor.ja_votou:
        request.session.pop("eleitor_id", None)
        request.session.pop("eleicao_id", None)
        return render(
            request,
            "eleicao/voto_ja_registrado.html",
            {"eleicao": eleitor.eleicao},
        )
    if eleitor.eleicao.status_atual != "ABERTA":
        request.session.pop("eleitor_id", None)
        request.session.pop("eleicao_id", None)
        if eleitor.eleicao.status_atual == "ENCERRADA":
            return _render_resultado_publico(request, eleitor.eleicao)
        return redirect("eleicao:inicio")

    chapas = Chapa.objects.filter(eleicao_id=eleicao_id)
    if request.method == "GET":
        return render(request, "eleicao/urna.html", {"chapas": chapas})

    escolha = request.POST.get("escolha", "")
    chapa = None
    if escolha.startswith("chapa:"):
        try:
            chapa_id = int(escolha.partition(":")[2])
        except ValueError:
            chapa_id = None
        chapa = get_object_or_404(chapas, pk=chapa_id)
        tipo_voto = Voto.Tipo.VALIDO
    elif escolha not in (Voto.Tipo.BRANCO, Voto.Tipo.NULO):
        messages.error(request, "Selecione uma chapa, voto branco ou voto nulo.")
        return render(request, "eleicao/urna.html", {"chapas": chapas}, status=400)
    else:
        tipo_voto = escolha

    with transaction.atomic():
        eleitor = Eleitor.objects.select_for_update().select_related("eleicao").get(pk=eleitor_id)
        Eleicao.objects.select_for_update().get(pk=eleitor.eleicao_id)
        if eleitor.ja_votou:
            request.session.pop("eleitor_id", None)
            request.session.pop("eleicao_id", None)
            messages.info(request, "Você já votou nesta eleição.")
            return render(
                request,
                "eleicao/voto_ja_registrado.html",
                {"eleicao": eleitor.eleicao},
            )
        if eleitor.eleicao.status_atual != "ABERTA":
            request.session.pop("eleitor_id", None)
            request.session.pop("eleicao_id", None)
            return redirect("eleicao:inicio")
        eleitor.ja_votou = True
        eleitor.save(update_fields=("ja_votou",))
        voto = Voto.objects.create(
            eleicao=eleitor.eleicao,
            chapa=chapa,
            tipo_voto=tipo_voto,
        )

    request.session.pop("eleitor_id", None)
    request.session.pop("eleicao_id", None)
    request.session.pop("token_votacao", None)
    return redirect("eleicao:comprovante", codigo=voto.codigo_comprovante)


@require_GET
def inicio(request):
    return redirect("eleicao:painel_login")


@require_GET
def auditoria_publica(request, eleicao_id=None):
    contexto_versao = _obter_identificacao_versao()
    if eleicao_id is None:
        eleicoes = Eleicao.objects.filter(
            Q(ativa=False) | Q(data_fim__lt=timezone.now())
        ).order_by("-data_fim", "-pk")
        return render(
            request,
            "eleicao/auditoria.html",
            {"eleicoes": eleicoes, **contexto_versao},
        )

    eleicao = get_object_or_404(Eleicao, pk=eleicao_id)
    timeline_eventos, timeline_tem_elos_ocultos = _construir_timeline_eleicao(
        eleicao
    )
    if eleicao.status_atual != "ENCERRADA":
        aviso_status = (
            "A votação ainda está agendada."
            if eleicao.status_atual == "AGENDADA"
            else "A votação ainda está em andamento. Os resultados serão publicados após o encerramento."
        )
        return render(
            request,
            "eleicao/auditoria.html",
            {
                "eleicao": eleicao,
                "aviso_status": aviso_status,
                "timeline_eventos": timeline_eventos,
                "timeline_tem_elos_ocultos": timeline_tem_elos_ocultos,
                **contexto_versao,
            },
        )

    dados_boletim = obter_dados_boletim(eleicao)
    integridade = verificar_integridade_votos(eleicao)
    comprovantes = dados_boletim["comprovantes_ordenados"]
    verificacao = None
    if "verificar" in request.GET:
        valor_busca = request.GET.get("verificar", "").strip()
        if not valor_busca:
            verificacao = {
                "valida": False,
                "mensagem": "Informe um hash SHA-256 ou código de comprovante.",
            }
        elif valor_busca.casefold() == dados_boletim["assinatura_digital"].casefold():
            verificacao = {
                "valida": True,
                "mensagem": "Hash SHA-256 confirmado para este Boletim de Urna.",
            }
        elif valor_busca.upper() in comprovantes:
            verificacao = {
                "valida": True,
                "mensagem": "Comprovante encontrado no Boletim de Urna.",
            }
        else:
            verificacao = {
                "valida": False,
                "mensagem": "Hash ou comprovante não encontrado neste Boletim de Urna.",
            }

    return render(
        request,
        "eleicao/auditoria.html",
        {
            "eleicao": eleicao,
            "dados_boletim": dados_boletim,
            "comprovantes": comprovantes,
            "verificacao": verificacao,
            "timeline_eventos": timeline_eventos,
            "timeline_tem_elos_ocultos": timeline_tem_elos_ocultos,
            **integridade,
            **contexto_versao,
        },
    )


@require_GET
def comprovante(request, codigo):
    voto = get_object_or_404(
        Voto.objects.select_related("eleicao"),
        codigo_comprovante=codigo,
    )
    return render(request, "eleicao/comprovante.html", {"voto": voto})
