from datetime import timedelta
from io import StringIO
from smtplib import SMTPAuthenticationError, SMTPDataError, SMTPException
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Eleicao, Eleitor
from .services.email_service import enviar_convite


class EnviarConviteTests(TestCase):
    def setUp(self):
        agora = timezone.now()
        self.eleicao = Eleicao.objects.create(
            titulo="Eleição do DCE",
            descricao="Eleição para o diretório central.",
            data_inicio=agora - timedelta(hours=1),
            data_fim=agora + timedelta(hours=1),
        )
        self.eleitor = Eleitor.objects.create(
            eleicao=self.eleicao,
            nome="Ana da Silva",
            email="ana@example.com",
            matricula="12345",
        )

    @override_settings(
        DEFAULT_FROM_EMAIL="Comissão Eleitoral <comissao@example.com>",
        SITE_BASE_URL="https://votos.example.org",
    )
    @patch("eleicao.services.email_service.get_connection")
    def test_envia_email_multialternatives_com_link_e_reutiliza_conexao(
        self, get_connection_mock
    ):
        conexao = get_connection_mock.return_value
        conexao.send_messages.return_value = 1

        resultado = enviar_convite(self.eleitor, connection=conexao)

        self.assertEqual(resultado, 1)
        get_connection_mock.assert_not_called()
        mensagens = conexao.send_messages.call_args.args[0]
        self.assertEqual(len(mensagens), 1)
        mensagem = mensagens[0]
        link = f"https://votos.example.org/votar/{self.eleitor.token_convite}/"
        self.assertEqual(mensagem.from_email, "Comissão Eleitoral <comissao@example.com>")
        self.assertEqual(mensagem.to, [self.eleitor.email])
        self.assertIn(link, mensagem.body)
        self.assertIn("Comissão Eleitoral", mensagem.body)
        self.assertEqual(len(mensagem.alternatives), 1)
        self.assertIn(link, mensagem.alternatives[0].content)
        self.assertIn("Comissão Eleitoral", mensagem.alternatives[0].content)
        self.assertEqual(mensagem.alternatives[0].mimetype, "text/html")

    @override_settings(
        DEFAULT_FROM_EMAIL="comissao@example.com",
        SITE_BASE_URL="https://votos.example.org/",
    )
    @patch("eleicao.services.email_service.get_connection")
    def test_usa_site_base_url_configurada_e_abre_conexao_padrao(self, get_connection_mock):
        conexao = get_connection_mock.return_value
        conexao.send_messages.return_value = 1

        enviar_convite(self.eleitor)

        mensagem = conexao.send_messages.call_args.args[0][0]
        self.assertIn(
            f"https://votos.example.org/votar/{self.eleitor.token_convite}/",
            mensagem.body,
        )
        get_connection_mock.assert_called_once_with()

    @override_settings(
        DEFAULT_FROM_EMAIL="comissao@example.com",
        SITE_BASE_URL="https://votos.example.org",
    )
    @patch("eleicao.services.email_service.get_connection")
    def test_convite_agendado_informa_horario_de_abertura(self, get_connection_mock):
        data_inicio = timezone.now() + timedelta(days=1)
        eleicao_agendada = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="",
            data_inicio=data_inicio,
            data_fim=data_inicio + timedelta(hours=2),
        )
        eleitor = Eleitor.objects.create(
            eleicao=eleicao_agendada,
            nome="Eleitor convidado",
            email="convidado@example.com",
            matricula="54321",
        )
        conexao = get_connection_mock.return_value
        conexao.send_messages.return_value = 1

        enviar_convite(eleitor)

        mensagem = conexao.send_messages.call_args.args[0][0]
        data_formatada = timezone.localtime(data_inicio).strftime("%d/%m/%Y às %H:%M")
        self.assertIn(data_formatada, mensagem.body)
        self.assertIn("Este link ficará disponível a partir desse horário", mensagem.body)
        self.assertIn("Abrir convite", mensagem.body)

    @override_settings(DEFAULT_FROM_EMAIL="")
    def test_exige_remetente_configurado(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "DEFAULT_FROM_EMAIL"):
            enviar_convite(self.eleitor, "https://votos.example.org")

    @override_settings(DEFAULT_FROM_EMAIL="comissao@example.com", SITE_BASE_URL="")
    def test_exige_url_base_configurada(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "SITE_BASE_URL"):
            enviar_convite(self.eleitor)


class DispararConvitesTests(TestCase):
    def setUp(self):
        agora = timezone.now()
        self.eleicao = Eleicao.objects.create(
            titulo="Eleição do DCE",
            descricao="Eleição para o diretório central.",
            data_inicio=agora - timedelta(hours=1),
            data_fim=agora + timedelta(hours=1),
        )
        self.eleitores = [
            Eleitor.objects.create(
                eleicao=self.eleicao,
                nome=f"Eleitor {indice}",
                email=f"eleitor{indice}@example.com",
                matricula=str(indice),
            )
            for indice in range(1, 4)
        ]

    def configurar_smtp(self):
        return override_settings(
            EMAIL_HOST_USER="dce@example.com",
            EMAIL_HOST_PASSWORD="senha-de-app-de-teste",
            DEFAULT_FROM_EMAIL="Comissão Eleitoral <dce@example.com>",
            SITE_BASE_URL="https://votos.example.org",
        )

    def criar_conexao(self):
        conexao = Mock()
        conexao.open.return_value = None
        conexao.close.return_value = None
        return conexao

    def test_envia_com_conexao_unica_e_intervalo_de_0_4_segundos(self):
        conexao = self.criar_conexao()
        saida = StringIO()
        with (
            self.configurar_smtp(),
            patch(
                "eleicao.management.commands.disparar_convites.get_connection",
                return_value=conexao,
            ) as get_connection_mock,
            patch("eleicao.management.commands.disparar_convites.enviar_convite") as enviar,
            patch("eleicao.management.commands.disparar_convites.time.sleep") as dormir,
        ):
            enviar.return_value = 1
            call_command("disparar_convites", "--limite=2", stdout=saida)

        get_connection_mock.assert_called_once_with()
        conexao.open.assert_called_once_with()
        conexao.close.assert_called_once_with()
        self.assertEqual(enviar.call_count, 2)
        self.assertTrue(
            all(
                chamada.args[1] == "https://votos.example.org"
                and chamada.kwargs["connection"] is conexao
                for chamada in enviar.call_args_list
            )
        )
        dormir.assert_called_once_with(0.4)
        self.assertEqual(Eleitor.objects.filter(email_enviado=True).count(), 2)
        self.assertTrue(
            all(eleitor.data_envio for eleitor in Eleitor.objects.filter(email_enviado=True))
        )
        self.assertIn("2 aceitos pelo SMTP", saida.getvalue())

    def test_envia_eleicao_agendada_e_nao_envia_a_quem_ja_votou(self):
        agora = timezone.now()
        eleicao_agendada = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="Convites enviados antes da abertura.",
            data_inicio=agora + timedelta(days=1),
            data_fim=agora + timedelta(days=2),
        )
        eleitor_pendente = Eleitor.objects.create(
            eleicao=eleicao_agendada,
            nome="Eleitor pendente",
            email="pendente@example.com",
            matricula="futuro-1",
        )
        eleitor_votante = Eleitor.objects.create(
            eleicao=eleicao_agendada,
            nome="Eleitor que votou",
            email="votante@example.com",
            matricula="futuro-2",
            ja_votou=True,
        )
        conexao = self.criar_conexao()
        with (
            self.configurar_smtp(),
            patch(
                "eleicao.management.commands.disparar_convites.get_connection",
                return_value=conexao,
            ),
            patch(
                "eleicao.management.commands.disparar_convites.enviar_convite",
                return_value=1,
            ) as enviar,
            patch("eleicao.management.commands.disparar_convites.time.sleep"),
        ):
            call_command(
                "disparar_convites",
                eleicao=eleicao_agendada.pk,
                stdout=StringIO(),
            )

        self.assertEqual(enviar.call_count, 1)
        self.assertEqual(enviar.call_args.args[0].pk, eleitor_pendente.pk)
        eleitor_pendente.refresh_from_db()
        eleitor_votante.refresh_from_db()
        self.assertTrue(eleitor_pendente.email_enviado)
        self.assertFalse(eleitor_votante.email_enviado)
        conexao.close.assert_called_once_with()

    def test_registra_falha_na_mensagem_e_continua_com_as_demais(self):
        conexao = self.criar_conexao()
        with (
            self.configurar_smtp(),
            patch(
                "eleicao.management.commands.disparar_convites.get_connection",
                return_value=conexao,
            ),
            patch(
                "eleicao.management.commands.disparar_convites.enviar_convite",
                side_effect=[SMTPException("falha temporária"), 1],
            ) as enviar,
            patch("eleicao.management.commands.disparar_convites.time.sleep"),
        ):
            call_command("disparar_convites", "--limite=2", stdout=StringIO())

        primeiro, segundo = Eleitor.objects.order_by("pk")[:2]
        self.assertIn("falha temporária", primeiro.erro_envio)
        self.assertFalse(primeiro.email_enviado)
        self.assertTrue(segundo.email_enviado)
        self.assertEqual(enviar.call_count, 2)
        conexao.close.assert_called_once_with()

    def test_falha_de_autenticacao_ao_abrir_registra_pendentes_e_interrompe(self):
        conexao = self.criar_conexao()
        conexao.open.side_effect = SMTPAuthenticationError(535, b"autenticacao recusada")
        with (
            self.configurar_smtp(),
            patch(
                "eleicao.management.commands.disparar_convites.get_connection",
                return_value=conexao,
            ),
            patch("eleicao.management.commands.disparar_convites.enviar_convite") as enviar,
        ):
            with self.assertRaisesMessage(CommandError, "Falha ao abrir conexão SMTP"):
                call_command("disparar_convites", stdout=StringIO(), stderr=StringIO())

        enviar.assert_not_called()
        self.assertEqual(
            Eleitor.objects.filter(erro_envio__contains="autenticacao recusada").count(),
            len(self.eleitores),
        )
        conexao.close.assert_called_once_with()

    def test_interrompe_imediatamente_em_erros_de_limite_smtp(self):
        erros_de_limite = (
            SMTPDataError(421, b"limite temporario"),
            SMTPDataError(450, b"limite temporario"),
            SMTPDataError(550, b"5.4.5 limite diario"),
        )
        for erro in erros_de_limite:
            with self.subTest(erro=erro.smtp_code):
                Eleitor.objects.update(email_enviado=False, erro_envio="")
                conexao = self.criar_conexao()
                with (
                    self.configurar_smtp(),
                    patch(
                        "eleicao.management.commands.disparar_convites.get_connection",
                        return_value=conexao,
                    ),
                    patch(
                        "eleicao.management.commands.disparar_convites.enviar_convite",
                        side_effect=erro,
                    ) as enviar,
                    patch("eleicao.management.commands.disparar_convites.time.sleep") as dormir,
                ):
                    call_command(
                        "disparar_convites",
                        stdout=StringIO(),
                        stderr=StringIO(),
                    )

                self.assertEqual(enviar.call_count, 1)
                self.assertTrue(Eleitor.objects.get(pk=self.eleitores[0].pk).erro_envio)
                self.assertEqual(
                    Eleitor.objects.filter(erro_envio="").count(),
                    len(self.eleitores) - 1,
                )
                dormir.assert_not_called()
                conexao.close.assert_called_once_with()

    def test_rejeita_configuracao_smtp_ausente(self):
        with override_settings(
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            DEFAULT_FROM_EMAIL="",
            SITE_BASE_URL="",
        ):
            with self.assertRaisesMessage(CommandError, "EMAIL_HOST_USER"):
                call_command("disparar_convites", stdout=StringIO())

    def test_nao_abre_conexao_quando_nao_ha_convites_pendentes(self):
        Eleitor.objects.update(email_enviado=True)
        with (
            self.configurar_smtp(),
            patch("eleicao.management.commands.disparar_convites.get_connection") as get_connection_mock,
        ):
            call_command("disparar_convites", stdout=StringIO())

        get_connection_mock.assert_not_called()
