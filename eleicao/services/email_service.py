from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils import timezone
from django.utils.html import format_html


def enviar_convite(eleitor, base_url=None, connection=None):
    base_url = base_url or settings.SITE_BASE_URL
    if not settings.DEFAULT_FROM_EMAIL:
        raise ImproperlyConfigured("A configuração DEFAULT_FROM_EMAIL não está definida.")
    if not base_url:
        raise ImproperlyConfigured("A configuração SITE_BASE_URL não está definida.")

    link_votacao = f"{base_url.rstrip('/')}/votar/{eleitor.token_convite}/"
    if eleitor.eleicao.status_atual == "AGENDADA":
        data_abertura = timezone.localtime(eleitor.eleicao.data_inicio).strftime(
            "%d/%m/%Y às %H:%M"
        )
        aviso_html = format_html(
            "<p>A votação será aberta em <strong>{}</strong>. "
            "Este link ficará disponível a partir desse horário.</p>",
            data_abertura,
        )
        aviso_texto = (
            f"A votação será aberta em {data_abertura}. "
            "Este link ficará disponível a partir desse horário.\n\n"
        )
        texto_link = "Abrir convite"
    else:
        aviso_html = ""
        aviso_texto = ""
        texto_link = "Acessar urna de votação"

    corpo_html = format_html(
        "<p>Olá, {}.</p>"
        "<p>Você está convidado(a) a participar da eleição <strong>{}</strong>.</p>"
        "{}"
        '<p><a href="{}">{}</a></p>'
        "<p>Atenciosamente,<br>Comissão Eleitoral</p>",
        eleitor.nome,
        eleitor.eleicao.titulo,
        aviso_html,
        link_votacao,
        texto_link,
    )
    corpo_texto = (
        f"Olá, {eleitor.nome}.\n\n"
        f"Você está convidado(a) a participar da eleição {eleitor.eleicao.titulo}.\n"
        f"{aviso_texto}"
        f"{texto_link}: {link_votacao}\n\n"
        "Atenciosamente,\nComissão Eleitoral"
    )
    mensagem = EmailMultiAlternatives(
        subject=f"Convite para votação: {eleitor.eleicao.titulo}",
        body=corpo_texto,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[eleitor.email],
        connection=connection if connection is not None else get_connection(),
    )
    mensagem.attach_alternative(str(corpo_html), "text/html")
    return mensagem.send(fail_silently=False)
