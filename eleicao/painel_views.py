import csv
import io
import os
import random
from smtplib import SMTPException
import zipfile
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .forms import (
    ChapaForm,
    EleicaoForm,
    ImportacaoArquivoForm,
    ImportacaoTextoForm,
)
from .models import Chapa, Eleicao, Eleitor, Voto
from .services.auditoria_eleicao import obter_dados_boletim
from .services.email_service import enviar_convite


@require_http_methods(["GET", "POST"])
def painel_login(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect(settings.LOGIN_REDIRECT_URL)

    formulario = AuthenticationForm(
        request,
        data=request.POST if request.method == "POST" else None,
    )
    for campo in formulario.fields.values():
        campo.widget.attrs["class"] = (
            "mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2.5 "
            "text-sm focus:border-indigo-600 focus:outline-none focus:ring-2 focus:ring-indigo-100"
        )
    if request.method == "POST" and formulario.is_valid():
        usuario = formulario.get_user()
        if not usuario.is_staff:
            formulario.add_error(None, "Este acesso é restrito à equipe da comissão.")
        else:
            login(request, usuario)
            return redirect(settings.LOGIN_REDIRECT_URL)
    return render(request, "painel/login.html", {"formulario": formulario})


@staff_member_required(login_url=settings.LOGIN_URL)
@require_POST
def painel_logout(request):
    logout(request)
    messages.success(request, "Sessão encerrada.")
    return redirect(settings.LOGOUT_REDIRECT_URL)


@staff_member_required(login_url=settings.LOGIN_URL)
def painel(request):
    agora = timezone.now()
    eleicoes_ativas = Eleicao.objects.filter(
        ativa=True,
        data_fim__gte=agora,
    ).order_by("-data_inicio")
    eleicao_id = request.GET.get("eleicao")
    if eleicao_id:
        eleicao = get_object_or_404(eleicoes_ativas, pk=eleicao_id)
    else:
        eleicao = eleicoes_ativas.filter(data_inicio__lte=agora).first()
        if eleicao is None:
            eleicao = eleicoes_ativas.order_by("data_inicio").first()

    contexto = {
        "eleicoes_ativas": eleicoes_ativas,
        "eleicao": eleicao,
    }
    if eleicao:
        eleitores = Eleitor.objects.filter(eleicao=eleicao)
        total_eleitores = eleitores.count()
        eleitores_votantes = eleitores.filter(ja_votou=True).count()
        contexto.update(
            {
                "total_eleitores": total_eleitores,
                "total_enviados": eleitores.filter(email_enviado=True).count(),
                "total_erros": eleitores.exclude(erro_envio="").count(),
                "total_votos": Voto.objects.filter(eleicao=eleicao).count(),
                "participacao": (
                    round(eleitores_votantes * 100 / total_eleitores, 1)
                    if total_eleitores
                    else 0
                ),
            }
        )
    return render(request, "painel/dashboard.html", contexto)


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_eleicoes(request):
    agora = timezone.now()
    abertas = Eleicao.objects.filter(
        ativa=True,
        data_inicio__lte=agora,
        data_fim__gte=agora,
    ).order_by("-data_inicio", "-pk")
    eleicao_destaque = abertas.first()
    outras_abertas = abertas[1:]
    eleicoes_agendadas = Eleicao.objects.filter(
        ativa=True,
        data_inicio__gt=agora,
        data_fim__gte=agora,
    ).order_by("data_inicio", "pk")
    eleicoes_encerradas = Eleicao.objects.filter(
        Q(ativa=False) | Q(data_fim__lt=agora)
    ).order_by("-data_inicio", "-pk")
    return render(
        request,
        "painel/eleicoes.html",
        {
            "eleicao_destaque": eleicao_destaque,
            "outras_abertas": outras_abertas,
            "eleicoes_agendadas": eleicoes_agendadas,
            "eleicoes_encerradas": eleicoes_encerradas,
        },
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_eleicao_detalhes(request, eleicao_id):
    eleicao = get_object_or_404(Eleicao, pk=eleicao_id)
    eleitores = Eleitor.objects.filter(eleicao=eleicao)
    total_votantes = eleitores.filter(ja_votou=True).count()
    return render(
        request,
        "painel/eleicao_detalhes.html",
        {
            "eleicao": eleicao,
            "chapas": Chapa.objects.filter(eleicao=eleicao).order_by("numero"),
            "total_eleitores": eleitores.count(),
            "total_enviados": eleitores.filter(email_enviado=True).count(),
            "total_erros": eleitores.exclude(erro_envio="").count(),
            "total_votantes": total_votantes,
            "total_votos": Voto.objects.filter(eleicao=eleicao).count(),
        },
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_eleicao_form(request, eleicao_id=None):
    instancia = get_object_or_404(Eleicao, pk=eleicao_id) if eleicao_id else None
    if instancia and instancia.status_atual != "AGENDADA":
        messages.error(request, "Esta eleição não pode ser alterada após o início.")
        return redirect("eleicao:painel_eleicoes")
    formulario = EleicaoForm(
        request.POST if request.method == "POST" else None,
        instance=instancia,
    )
    if request.method == "POST" and formulario.is_valid():
        try:
            eleicao = formulario.save()
        except ValidationError as erro:
            formulario.add_error(None, erro)
        else:
            messages.success(request, f"Eleição “{eleicao.titulo}” salva.")
            return redirect(
                "eleicao:painel_eleicao_detalhes",
                eleicao_id=eleicao.pk,
            )
    return render(
        request,
        "painel/eleicao_form.html",
        {"formulario": formulario, "eleicao": instancia},
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_chapas(request):
    eleicoes = Eleicao.objects.order_by("-data_inicio")
    eleicao_id = request.GET.get("eleicao", "")
    eleicao_selecionada = (
        get_object_or_404(Eleicao, pk=eleicao_id) if eleicao_id else None
    )
    chapas = Chapa.objects.select_related("eleicao").order_by(
        "eleicao__data_inicio", "numero"
    )
    if eleicao_selecionada:
        chapas = chapas.filter(eleicao=eleicao_selecionada)
    else:
        chapas = chapas.none()
    return render(
        request,
        "painel/chapas.html",
        {
            "chapas": chapas,
            "eleicoes": eleicoes,
            "eleicao_selecionada": eleicao_selecionada,
        },
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_chapa_form(request, chapa_id=None):
    instancia = get_object_or_404(Chapa, pk=chapa_id) if chapa_id else None
    eleicao_contexto = (
        get_object_or_404(Eleicao, pk=request.GET["eleicao"])
        if request.GET.get("eleicao")
        else None
    )
    if instancia and instancia.eleicao.status_atual != "AGENDADA":
        messages.error(request, "Esta chapa não pode ser alterada após o início da eleição.")
        return redirect("eleicao:painel_chapas")
    formulario = ChapaForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=instancia,
        initial={"eleicao": eleicao_contexto} if eleicao_contexto else None,
    )
    if eleicao_contexto:
        formulario.fields["eleicao"].queryset = Eleicao.objects.filter(
            pk=eleicao_contexto.pk
        )
    if request.method == "POST" and formulario.is_valid():
        try:
            chapa = formulario.save()
        except ValidationError as erro:
            formulario.add_error(None, erro)
        else:
            messages.success(request, f"Chapa “{chapa.nome}” salva.")
            destino = reverse("eleicao:painel_chapas")
            eleicao_id = request.GET.get("eleicao")
            if eleicao_id:
                destino = f"{destino}?eleicao={eleicao_id}"
            return redirect(destino)
    return render(
        request,
        "painel/chapa_form.html",
        {
            "formulario": formulario,
            "chapa": instancia,
            "eleicao": eleicao_contexto or (instancia.eleicao if instancia else None),
        },
    )


def _celula_inicia_com_caractere_perigoso(valor):
    return str(valor).lstrip(" ").startswith(("\t", "\r", "\n", "=", "+", "-", "@"))


def _normalizar_linhas(linhas, permitir_sem_cabecalho=False):
    if not linhas:
        raise ValueError("O arquivo está vazio.")
    cabecalhos = [
        str(valor).lstrip("\ufeff").strip().strip("`'\"").strip().lower()
        if valor is not None
        else ""
        for valor in linhas[0]
    ]
    obrigatorios = {"nome", "email", "matricula"}
    tem_cabecalho = obrigatorios.issubset(cabecalhos)
    if not tem_cabecalho and not permitir_sem_cabecalho:
        raise ValueError(
            "Cabeçalho inválido: a primeira linha precisa conter nome, email e matricula. "
            "Cole somente o bloco de dados, sem as instruções ou texto adicional."
        )
    if not tem_cabecalho:
        if len(linhas[0]) < 3:
            raise ValueError(
                "Cada linha deve conter nome, e-mail e matrícula nas três primeiras colunas."
            )
        cabecalhos = ["nome", "email", "matricula"]

    registros = []
    primeira_linha_dados = 1 if tem_cabecalho else 0
    for numero_linha, valores in enumerate(
        linhas[primeira_linha_dados:],
        start=primeira_linha_dados + 1,
    ):
        linha = {
            cabecalho: valores[indice] if indice < len(valores) else None
            for indice, cabecalho in enumerate(cabecalhos)
            if cabecalho
        }
        for campo, valor in linha.items():
            if valor is not None and _celula_inicia_com_caractere_perigoso(valor):
                raise ValueError(
                    f"A linha {numero_linha} contém um valor iniciado por caractere "
                    f"reservado para fórmulas na coluna {campo}."
                )
        if any(valor is not None and str(valor).strip() for valor in linha.values()):
            registros.append(linha)
    return registros


def _ler_linhas_coladas(texto):
    primeira_linha = next(
        (linha for linha in texto.splitlines() if linha.strip()),
        "",
    )
    delimitadores = (",", ";", "\t")
    delimitador = max(delimitadores, key=primeira_linha.count)
    if primeira_linha.count(delimitador) == 0:
        raise ValueError(
            "Informe as colunas de nome, e-mail e matrícula separadas por "
            "vírgula, ponto-e-vírgula ou tabulação."
        )
    try:
        linhas = list(csv.reader(io.StringIO(texto), delimiter=delimitador))
    except csv.Error as erro:
        raise ValueError("Não foi possível interpretar as linhas coladas.") from erro
    linhas = [
        linha
        for linha in linhas
        if any(valor is not None and str(valor).strip() for valor in linha)
    ]
    return _normalizar_linhas(linhas, permitir_sem_cabecalho=True)


def _ler_linhas_arquivo(arquivo):
    extensao = os.path.splitext(arquivo.name)[1].lower()
    if extensao == ".csv":
        try:
            texto = arquivo.read().decode("utf-8-sig")
        except UnicodeDecodeError as erro:
            raise ValueError("O arquivo CSV deve estar codificado em UTF-8.") from erro
        leitor = csv.reader(io.StringIO(texto))
        linhas = list(leitor)
    elif extensao == ".xlsx":
        try:
            pasta = load_workbook(arquivo, read_only=True, data_only=True)
        except (InvalidFileException, OSError, zipfile.BadZipFile) as erro:
            raise ValueError("Não foi possível ler o arquivo Excel enviado.") from erro
        try:
            linhas = list(pasta.active.iter_rows(values_only=True))
        finally:
            pasta.close()
    else:
        raise ValueError("Envie um arquivo .csv ou .xlsx.")

    return _normalizar_linhas(linhas)


def _importar_eleitores(eleicao, arquivo=None, texto=None):
    linhas = (
        _ler_linhas_arquivo(arquivo)
        if arquivo
        else _ler_linhas_coladas(texto or "")
    )
    resumo = {"importados": 0, "duplicados": 0, "invalidos": 0}
    validador_email = forms.EmailField()

    for linha in linhas:
        nome = str(linha.get("nome") or "").strip()
        email = str(linha.get("email") or "").strip()
        matricula = str(linha.get("matricula") or "").strip()
        if not nome or not email or not matricula:
            resumo["invalidos"] += 1
            continue
        try:
            email = validador_email.clean(email).lower()
        except ValidationError:
            resumo["invalidos"] += 1
            continue

        ja_existe = Eleitor.objects.filter(eleicao=eleicao).filter(
            Q(email__iexact=email) | Q(matricula=matricula)
        ).exists()
        if ja_existe:
            resumo["duplicados"] += 1
            continue

        try:
            with transaction.atomic():
                Eleitor.objects.create(
                    eleicao=eleicao,
                    nome=nome,
                    email=email,
                    matricula=matricula,
                )
        except IntegrityError:
            resumo["duplicados"] += 1
        else:
            resumo["importados"] += 1
    return resumo


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_importar(request):
    eleicao_contexto = (
        get_object_or_404(Eleicao, pk=request.GET["eleicao"])
        if request.GET.get("eleicao")
        else None
    )
    initial = {"eleicao": eleicao_contexto} if eleicao_contexto else None
    formulario_arquivo = ImportacaoArquivoForm(prefix="arquivo", initial=initial)
    formulario_texto = ImportacaoTextoForm(prefix="texto", initial=initial)
    if eleicao_contexto:
        eleicao_permitida = Eleicao.objects.filter(pk=eleicao_contexto.pk)
        formulario_arquivo.fields["eleicao"].queryset = eleicao_permitida
        formulario_texto.fields["eleicao"].queryset = eleicao_permitida
    if request.method == "POST":
        origem = request.POST.get("origem")
        if origem == "arquivo":
            formulario_arquivo = ImportacaoArquivoForm(
                request.POST, request.FILES, prefix="arquivo"
            )
            formulario = formulario_arquivo
            campo_erro = "arquivo"
            valor_importacao = request.FILES.get("arquivo-arquivo")
        elif origem == "texto":
            formulario_texto = ImportacaoTextoForm(request.POST, prefix="texto")
            formulario = formulario_texto
            campo_erro = "texto"
            valor_importacao = formulario_texto.data.get("texto-texto", "")
        else:
            messages.error(request, "Escolha enviar um arquivo ou colar os dados.")
            destino = reverse("eleicao:painel_importar")
            eleicao_id = request.GET.get("eleicao")
            return redirect(f"{destino}?eleicao={eleicao_id}" if eleicao_id else destino)

        if eleicao_contexto:
            formulario.fields["eleicao"].queryset = Eleicao.objects.filter(
                pk=eleicao_contexto.pk
            )
        if not formulario.is_valid():
            return render(
                request,
                "painel/importar.html",
                {
                    "formulario_arquivo": formulario_arquivo,
                    "formulario_texto": formulario_texto,
                    "eleicao": eleicao_contexto,
                },
            )

        try:
            eleicao = formulario.cleaned_data["eleicao"]
            if eleicao.status_atual != "AGENDADA":
                formulario.add_error(
                    "eleicao",
                    "A importação de eleitores só pode ser feita antes do início da eleição.",
                )
                return render(
                    request,
                    "painel/importar.html",
                    {
                        "formulario_arquivo": formulario_arquivo,
                        "formulario_texto": formulario_texto,
                        "eleicao": eleicao,
                    },
                )
            if origem == "arquivo":
                resumo = _importar_eleitores(eleicao, arquivo=valor_importacao)
            else:
                resumo = _importar_eleitores(eleicao, texto=valor_importacao)
        except ValueError as erro:
            formulario.add_error(campo_erro, str(erro))
            return render(
                request,
                "painel/importar.html",
                {
                    "formulario_arquivo": formulario_arquivo,
                    "formulario_texto": formulario_texto,
                    "eleicao": eleicao_contexto,
                },
                status=400,
            )

        messages.success(
            request,
            "Importação concluída: "
            f"{resumo['importados']} importados, "
            f"{resumo['duplicados']} duplicados ignorados e "
            f"{resumo['invalidos']} inválidos.",
        )
        if resumo["importados"]:
            pendentes = Eleitor.objects.filter(
                eleicao=eleicao,
                email_enviado=False,
                ja_votou=False,
            ).count()
            if pendentes:
                try:
                    _executar_disparo_convites(eleicao, min(pendentes, 80))
                except CommandError as erro:
                    messages.warning(
                        request,
                        f"Os eleitores foram cadastrados, mas o envio automático "
                        f"não foi concluído: {erro}",
                    )
                else:
                    ainda_pendentes = Eleitor.objects.filter(
                        eleicao=eleicao,
                        email_enviado=False,
                        ja_votou=False,
                    ).count()
                    aceitos = pendentes - ainda_pendentes
                    if ainda_pendentes:
                        falhas = Eleitor.objects.filter(
                            eleicao=eleicao,
                            email_enviado=False,
                            ja_votou=False,
                        ).exclude(erro_envio="").count()
                        messages.warning(
                            request,
                            f"Convites processados: {aceitos} aceitos pelo SMTP; "
                            f"{ainda_pendentes} continuam pendentes, {falhas} com erro. "
                            "Consulte a lista de eleitores para ver os detalhes.",
                        )
                    else:
                        messages.info(
                            request,
                            f"Os {aceitos} convites pendentes foram aceitos pelo SMTP.",
                        )
        return redirect("eleicao:painel_eleicao_detalhes", eleicao_id=eleicao.pk)
    return render(
        request,
        "painel/importar.html",
        {
            "formulario_arquivo": formulario_arquivo,
            "formulario_texto": formulario_texto,
            "eleicao": eleicao_contexto,
        },
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_modelo_csv(request):
    resposta = HttpResponse(content_type="text/csv; charset=utf-8")
    resposta.write("\ufeff")
    resposta["Content-Disposition"] = (
        'attachment; filename="modelo-importacao-eleitores.csv"'
    )
    escritor = csv.writer(resposta)
    escritor.writerow(("nome", "email", "matricula"))
    escritor.writerow(("Ana Silva", "ana@example.com", "001234"))
    return resposta


def _url_lista_eleitores(request):
    origem = request.GET if request.method == "GET" else request.POST
    parametros = {
        chave: origem.get(chave)
        for chave in ("eleicao", "q", "status")
        if origem.get(chave)
    }
    query = urlencode(parametros)
    return reverse("eleicao:painel_eleitores") + (f"?{query}" if query else "")


def _registrar_falha_envio(eleitor, mensagem):
    eleitor.erro_envio = mensagem[:2000]
    eleitor.save(update_fields=("erro_envio",))


def _executar_disparo_convites(eleicao, limite):
    call_command(
        "disparar_convites",
        eleicao=eleicao.pk,
        limite=limite,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_eleitores(request):
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "enviar_pendentes":
            eleicao = get_object_or_404(Eleicao, pk=request.POST.get("eleicao"))
            if eleicao.status_atual == "ENCERRADA":
                messages.error(request, "Não é possível enviar convites de uma eleição encerrada.")
                return redirect(_url_lista_eleitores(request))

            pendentes = Eleitor.objects.filter(
                eleicao=eleicao,
                email_enviado=False,
                ja_votou=False,
            ).count()
            if not pendentes:
                messages.info(request, "Não há convites pendentes para esta eleição.")
                return redirect(_url_lista_eleitores(request))

            try:
                _executar_disparo_convites(eleicao, min(pendentes, 80))
            except CommandError as erro:
                messages.error(request, f"Não foi possível disparar os convites: {erro}")
            else:
                ainda_pendentes = Eleitor.objects.filter(
                    eleicao=eleicao,
                    email_enviado=False,
                    ja_votou=False,
                ).count()
                aceitos = pendentes - ainda_pendentes
                if ainda_pendentes:
                    falhas = Eleitor.objects.filter(
                        eleicao=eleicao,
                        email_enviado=False,
                        ja_votou=False,
                    ).exclude(erro_envio="").count()
                    messages.warning(
                        request,
                        f"Disparo concluído: {aceitos} aceitos pelo SMTP e "
                        f"{ainda_pendentes} ainda pendentes, {falhas} com erro. "
                        "Consulte a lista de eleitores para ver os detalhes.",
                    )
                else:
                    messages.success(
                        request,
                        f"Todos os {aceitos} convites pendentes foram aceitos pelo SMTP.",
                    )
            return redirect(_url_lista_eleitores(request))

        filtros_eleitor = {"pk": request.POST.get("eleitor_id")}
        if request.POST.get("eleicao"):
            filtros_eleitor["eleicao_id"] = request.POST["eleicao"]
        eleitor = get_object_or_404(Eleitor, **filtros_eleitor)
        if acao == "editar_email":
            try:
                if eleitor.ja_votou:
                    raise ValidationError("O e-mail não pode ser alterado após o eleitor votar.")
                novo_email = forms.EmailField().clean(request.POST.get("email", ""))
                with transaction.atomic():
                    if (
                        Eleitor.objects.filter(eleicao=eleitor.eleicao)
                        .exclude(pk=eleitor.pk)
                        .filter(email__iexact=novo_email)
                        .exists()
                    ):
                        raise ValidationError("Este e-mail já está cadastrado nesta eleição.")
                    eleitor.email = novo_email
                    eleitor.email_enviado = False
                    eleitor.data_envio = None
                    eleitor.erro_envio = ""
                    eleitor.save(
                        update_fields=("email", "email_enviado", "data_envio", "erro_envio")
                    )
            except ValidationError as erro:
                messages.error(request, "; ".join(erro.messages))
            except IntegrityError:
                messages.error(request, "Este e-mail já está cadastrado nesta eleição.")
            else:
                messages.success(request, "E-mail atualizado; o convite está pendente de envio.")
        elif acao == "reenviar":
            if eleitor.ja_votou:
                messages.error(request, "Não é possível reenviar o convite após o eleitor votar.")
            else:
                try:
                    enviados = enviar_convite(eleitor, settings.SITE_BASE_URL)
                except (SMTPException, OSError, ImproperlyConfigured) as erro:
                    mensagem = str(erro) or erro.__class__.__name__
                    _registrar_falha_envio(eleitor, mensagem)
                    messages.error(request, f"Não foi possível enviar o convite: {mensagem}")
                else:
                    if enviados:
                        eleitor.email_enviado = True
                        eleitor.data_envio = timezone.now()
                        eleitor.erro_envio = ""
                        eleitor.save(
                            update_fields=("email_enviado", "data_envio", "erro_envio")
                        )
                        messages.success(
                            request,
                            f"O servidor SMTP aceitou o convite para {eleitor.email}. "
                            "A aceitação não garante a entrega na caixa de entrada.",
                        )
                    else:
                        mensagem = "O backend SMTP não aceitou a mensagem para envio."
                        _registrar_falha_envio(eleitor, mensagem)
                        messages.error(request, f"Falha ao enviar convite: {mensagem}")
        else:
            messages.error(request, "Ação inválida.")
        return redirect(_url_lista_eleitores(request))

    busca = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    eleicao_id = request.GET.get("eleicao", "")
    eleicao = get_object_or_404(Eleicao, pk=eleicao_id) if eleicao_id else None
    eleitores = Eleitor.objects.select_related("eleicao")
    if eleicao:
        eleitores = eleitores.filter(eleicao=eleicao)
    else:
        eleitores = eleitores.none()
    if busca:
        eleitores = eleitores.filter(
            Q(nome__icontains=busca)
            | Q(email__icontains=busca)
            | Q(matricula__icontains=busca)
        )
    filtros = {
        "pendentes": Q(email_enviado=False),
        "enviados": Q(email_enviado=True),
        "erros": ~Q(erro_envio=""),
        "votaram": Q(ja_votou=True),
        "nao_votaram": Q(ja_votou=False),
    }
    if status in filtros:
        eleitores = eleitores.filter(filtros[status])
    paginas = Paginator(eleitores.order_by("nome", "pk"), 25)
    return render(
        request,
        "painel/eleitores.html",
        {
            "pagina": paginas.get_page(request.GET.get("page")),
            "busca": busca,
            "status_selecionado": status,
            "eleicoes": Eleicao.objects.order_by("-data_inicio"),
            "eleicao": eleicao,
            "total_pendentes": (
                Eleitor.objects.filter(
                    eleicao=eleicao,
                    email_enviado=False,
                    ja_votou=False,
                ).count()
                if eleicao
                else 0
            ),
        },
    )


def _opcoes_apuracao():
    agora = timezone.now()
    eleicoes_em_andamento = Eleicao.objects.filter(
        ativa=True,
        data_inicio__lte=agora,
        data_fim__gte=agora,
    ).order_by("-data_inicio", "-pk")
    eleicoes_encerradas = Eleicao.objects.filter(
        ativa=True,
        data_inicio__lte=agora,
        data_fim__lt=agora,
    )
    eleicoes_encerradas = eleicoes_encerradas.order_by("-data_fim", "-pk")
    ultima_encerrada = eleicoes_encerradas.first()
    opcoes = eleicoes_em_andamento
    if ultima_encerrada:
        opcoes = opcoes | Eleicao.objects.filter(pk=ultima_encerrada.pk)
        eleicoes_anteriores = eleicoes_encerradas.exclude(pk=ultima_encerrada.pk)
    else:
        eleicoes_anteriores = eleicoes_encerradas
    return (
        opcoes.order_by("-data_inicio", "-pk"),
        eleicoes_em_andamento,
        ultima_encerrada,
        eleicoes_anteriores,
    )


def _eleicao_encerrada(request):
    try:
        eleicao_id = int(request.GET.get("eleicao", ""))
    except (TypeError, ValueError) as erro:
        raise Http404 from erro
    eleicao = get_object_or_404(Eleicao, pk=eleicao_id)
    if eleicao.status_atual != "ENCERRADA":
        raise Http404
    return eleicao


def _painel_apuracao(request, csv_comparecimento=False):
    eleicao = _eleicao_encerrada(request)
    (
        _,
        eleicoes_em_andamento,
        ultima_eleicao_encerrada,
        eleicoes_anteriores,
    ) = _opcoes_apuracao()
    eleitores_presentes = Eleitor.objects.filter(eleicao=eleicao, ja_votou=True)
    if csv_comparecimento:
        resposta = HttpResponse(content_type="text/csv; charset=utf-8")
        resposta.write("\ufeff")
        resposta["Content-Disposition"] = (
            f'attachment; filename="comparecimento-eleicao-{eleicao.pk}.csv"'
        )
        escritor = csv.writer(resposta)
        escritor.writerow(("Nome", "Matrícula"))
        for eleitor in eleitores_presentes.order_by("nome", "matricula"):
            escritor.writerow(
                (_proteger_formula_csv(eleitor.nome), _proteger_formula_csv(eleitor.matricula))
            )
        return resposta

    resultado = obter_dados_boletim(eleicao)
    comprovantes = (
        resultado["comprovantes_ordenados"].copy()
        if resultado["integridade_ok"]
        else []
    )
    if resultado["integridade_ok"]:
        random.SystemRandom().shuffle(comprovantes)

    membros_comissao = [
        nome.strip()
        for nome in eleicao.membros_comissao.splitlines()
        if nome.strip()
    ]
    if not membros_comissao:
        membros_comissao = [None, None, None]

    return render(
        request,
        "painel/apuracao.html",
        {
            "eleicoes_em_andamento": eleicoes_em_andamento,
            "ultima_eleicao_encerrada": ultima_eleicao_encerrada,
            "eleicoes_anteriores": eleicoes_anteriores,
            "eleicao": eleicao,
            "data_emissao": timezone.localtime(timezone.now()),
            "membros_comissao": membros_comissao,
            "eleicao_historica_selecionada": bool(
                ultima_eleicao_encerrada
                and eleicao.pk != ultima_eleicao_encerrada.pk
            ),
            **resultado,
            "comprovantes": comprovantes,
        },
    )


def _proteger_formula_csv(valor):
    texto = str(valor)
    if _celula_inicia_com_caractere_perigoso(texto):
        return f"'{texto}"
    return texto


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_apuracao(request):
    (
        eleicoes,
        eleicoes_em_andamento,
        ultima_eleicao_encerrada,
        eleicoes_anteriores,
    ) = _opcoes_apuracao()
    eleicao_id = request.GET.get("eleicao")
    if not eleicao_id:
        return render(
            request,
            "painel/apuracao.html",
            {
                "eleicoes_em_andamento": eleicoes_em_andamento,
                "ultima_eleicao_encerrada": ultima_eleicao_encerrada,
                "eleicoes_anteriores": eleicoes_anteriores,
                "eleicao": None,
                "eleicao_historica_selecionada": False,
            },
        )
    try:
        eleicao_id = int(eleicao_id)
    except ValueError as erro:
        raise Http404 from erro
    eleicao = get_object_or_404(Eleicao, pk=eleicao_id)
    if eleicao.status_atual != "ENCERRADA":
        eleicao = get_object_or_404(eleicoes_em_andamento, pk=eleicao_id)
        return render(
            request,
            "painel/apuracao.html",
            {
                "eleicoes_em_andamento": eleicoes_em_andamento,
                "ultima_eleicao_encerrada": ultima_eleicao_encerrada,
                "eleicoes_anteriores": eleicoes_anteriores,
                "eleicao": eleicao,
                "eleicao_historica_selecionada": False,
            },
        )
    return _painel_apuracao(request)


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_zeresima(request):
    agora = timezone.now()
    eleicoes = Eleicao.objects.filter(
        ativa=True,
        data_fim__gte=agora,
    ).order_by("-data_inicio")
    eleicao_id = request.GET.get("eleicao", "")
    eleicao = get_object_or_404(eleicoes, pk=eleicao_id) if eleicao_id else None
    contexto = {
        "eleicoes": eleicoes,
        "eleicao": eleicao,
        "data_emissao": timezone.localtime(agora),
    }
    if eleicao:
        contexto.update(
            {
                "chapas": Chapa.objects.filter(eleicao=eleicao).order_by("numero"),
                "total_eleitores": Eleitor.objects.filter(eleicao=eleicao).count(),
                "total_votos": Voto.objects.filter(eleicao=eleicao).count(),
                "total_eleitores_que_votaram": Eleitor.objects.filter(
                    eleicao=eleicao,
                    ja_votou=True,
                ).count(),
                "membros_comissao": [
                    nome.strip()
                    for nome in eleicao.membros_comissao.splitlines()
                    if nome.strip()
                ],
            }
        )
    return render(request, "painel/zeresima.html", contexto)


@staff_member_required(login_url=settings.LOGIN_URL)
def painel_comparecimento_csv(request):
    return _painel_apuracao(request, csv_comparecimento=True)
