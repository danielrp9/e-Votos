import time
from smtplib import (
    SMTPAuthenticationError,
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
)

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from eleicao.models import Eleitor
from eleicao.services.email_service import enviar_convite


class Command(BaseCommand):
    help = "Envia convites de votação pendentes pelo servidor SMTP configurado."

    def add_arguments(self, parser):
        parser.add_argument("--limite", type=int, default=80)
        parser.add_argument(
            "--eleicao",
            type=int,
            help="Envia somente convites pendentes da eleição informada.",
        )

    @staticmethod
    def _parar_por_autenticacao_ou_limite(erro):
        if isinstance(erro, SMTPAuthenticationError):
            return True

        respostas = []
        if isinstance(erro, SMTPResponseException):
            respostas.append((erro.smtp_code, erro.smtp_error))
        if isinstance(erro, SMTPRecipientsRefused):
            respostas.extend(erro.recipients.values())

        for codigo, resposta in respostas:
            try:
                codigo = int(codigo)
            except (TypeError, ValueError):
                continue
            if isinstance(resposta, bytes):
                resposta = resposta.decode(errors="replace")
            if codigo in (421, 450) or (codigo == 550 and "5.4.5" in str(resposta)):
                return True
        return False

    @staticmethod
    def _registrar_falha(eleitor, erro):
        eleitor.erro_envio = str(erro)[:2000] or erro.__class__.__name__
        eleitor.save(update_fields=("erro_envio",))

    def handle(self, *args, **options):
        limite = options["limite"]
        if limite < 1:
            raise CommandError("--limite deve ser maior que zero.")
        if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
            raise CommandError("EMAIL_HOST_USER e EMAIL_HOST_PASSWORD devem estar configurados.")
        if not settings.DEFAULT_FROM_EMAIL:
            raise CommandError("A configuração DEFAULT_FROM_EMAIL não está definida.")
        if not settings.SITE_BASE_URL:
            raise CommandError("A configuração SITE_BASE_URL não está definida.")

        pendentes = Eleitor.objects.filter(
            eleicao__ativa=True,
            eleicao__data_fim__gte=timezone.now(),
            email_enviado=False,
            ja_votou=False,
        )
        if options["eleicao"] is not None:
            pendentes = pendentes.filter(eleicao_id=options["eleicao"])
        eleitores = list(
            pendentes.select_related("eleicao").order_by("pk")[:limite]
        )
        total_enviados = 0
        total_falhas = 0
        if not eleitores:
            self.stdout.write(self.style.SUCCESS("Nenhum convite pendente para envio."))
            return

        conexao = get_connection()
        try:
            try:
                conexao.open()
            except (SMTPException, OSError) as erro:
                for eleitor in eleitores:
                    self._registrar_falha(eleitor, erro)
                raise CommandError(f"Falha ao abrir conexão SMTP: {erro}") from erro

            for indice, eleitor in enumerate(eleitores):
                try:
                    enviados = enviar_convite(
                        eleitor,
                        settings.SITE_BASE_URL,
                        connection=conexao,
                    )
                except (SMTPException, OSError) as erro:
                    self._registrar_falha(eleitor, erro)
                    total_falhas += 1
                    self.stderr.write(f"Falha ao enviar para {eleitor.email}: {eleitor.erro_envio}")
                    if self._parar_por_autenticacao_ou_limite(erro):
                        self.stderr.write(
                            "Envio interrompido por falha de autenticação ou limite SMTP."
                        )
                        break
                else:
                    if enviados:
                        eleitor.email_enviado = True
                        eleitor.data_envio = timezone.now()
                        eleitor.erro_envio = ""
                        eleitor.save(update_fields=("email_enviado", "data_envio", "erro_envio"))
                        total_enviados += 1
                        self.stdout.write(f"SMTP aceitou o convite para {eleitor.email}.")
                    else:
                        erro = "O backend SMTP não aceitou a mensagem para envio."
                        self._registrar_falha(eleitor, erro)
                        total_falhas += 1
                        self.stderr.write(f"Falha ao enviar para {eleitor.email}: {erro}")

                if indice < len(eleitores) - 1:
                    time.sleep(0.4)
        finally:
            conexao.close()

        self.stdout.write(
            self.style.SUCCESS(
                f"Processamento concluído: {total_enviados} aceitos pelo SMTP, "
                f"{total_falhas} falhas."
            )
        )
