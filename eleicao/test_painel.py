import csv
import hashlib
import json
import re
from datetime import timedelta
from io import BytesIO, StringIO
from smtplib import SMTPDataError
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from PIL import Image
from .forms import ChapaForm
from .models import Chapa, Eleicao, Eleitor, Voto
from .painel_views import _normalizar_linhas


class PainelComissaoTests(TestCase):
    def setUp(self):
        usuario = get_user_model().objects.create_user(
            username="comissao", password="senha-segura", is_staff=True
        )
        self.usuario_staff = usuario
        self.client.force_login(usuario)
        agora = timezone.now()
        self.eleicao = Eleicao.objects.create(
            titulo="Eleição do DCE",
            descricao="Eleição para o diretório central.",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        self.chapa = Chapa.objects.create(
            eleicao=self.eleicao, numero=1, nome="Renova"
        )

    def criar_eleitor(self, nome="Ana Silva", email="ana@example.com", matricula="001"):
        return Eleitor.objects.create(
            eleicao=self.eleicao,
            nome=nome,
            email=email,
            matricula=matricula,
        )

    def test_painel_exige_usuario_staff(self):
        self.client.logout()

        resposta = self.client.get(reverse("eleicao:painel"))

        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.url.startswith(settings.LOGIN_URL))
        usuario_comum = get_user_model().objects.create_user(
            username="aluno", password="senha-segura", is_staff=False
        )
        self.client.force_login(usuario_comum)
        self.assertEqual(self.client.get(reverse("eleicao:painel_importar")).status_code, 302)

    def test_configuracoes_de_cookie_e_csrf_sao_endurecidas(self):
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertEqual(settings.SECURE_REFERRER_POLICY, "same-origin")

        cliente_com_csrf = Client(enforce_csrf_checks=True)
        cliente_com_csrf.force_login(self.usuario_staff)
        resposta = cliente_com_csrf.post(reverse("eleicao:painel_eleicao_nova"), {})
        self.assertEqual(resposta.status_code, 403)

    def test_csrf_origin_null_exige_referer_same_origin_e_token(self):
        cliente = Client(enforce_csrf_checks=True)
        url_login = reverse("eleicao:painel_login")
        resposta_get = cliente.get(url_login)
        token = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            resposta_get.content,
        )
        self.assertIsNotNone(token)

        dados = {
            "csrfmiddlewaretoken": token.group(1).decode(),
            "username": "usuario-inexistente",
            "password": "senha-invalida",
        }
        resposta_post = cliente.post(
            url_login,
            dados,
            HTTP_ORIGIN="null",
            HTTP_REFERER=f"http://testserver{url_login}",
        )
        self.assertEqual(resposta_post.status_code, 200)

        resposta_referer_externo = cliente.post(
            url_login,
            dados,
            HTTP_ORIGIN="null",
            HTTP_REFERER="http://origem-maliciosa.example/",
        )
        self.assertEqual(resposta_referer_externo.status_code, 403)

    def test_login_dedicado_autentica_apenas_staff_e_logout(self):
        self.client.logout()
        login_url = reverse("eleicao:painel_login")

        resposta = self.client.get(login_url)
        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "painel/login.html")
        self.assertTemplateUsed(resposta, "painel/base_painel.html")
        self.assertNotContains(resposta, '<header class="no-print sticky')
        self.assertContains(resposta, 'class="mx-auto w-full max-w-md rounded')
        self.assertContains(resposta, "min-h-screen max-w-5xl items-center justify-center")
        self.assertContains(resposta, "Auditoria Pública")

        resposta = self.client.post(
            login_url,
            {"username": "comissao", "password": "senha-segura"},
        )
        self.assertRedirects(resposta, settings.LOGIN_REDIRECT_URL)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.usuario_staff.pk)
        self.assertEqual(self.client.get(reverse("eleicao:painel")).status_code, 200)

        resposta = self.client.post(reverse("eleicao:painel_logout"))
        self.assertRedirects(resposta, settings.LOGOUT_REDIRECT_URL)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_dedicado_recusa_usuario_sem_permissao_staff(self):
        get_user_model().objects.create_user(
            username="aluno", password="senha-segura", is_staff=False
        )
        self.client.logout()

        resposta = self.client.post(
            reverse("eleicao:painel_login"),
            {"username": "aluno", "password": "senha-segura"},
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "restrito à equipe da comissão")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_home_redireciona_para_login_do_painel(self):
        resposta = self.client.get(reverse("eleicao:inicio"))

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta["Location"], reverse("eleicao:painel_login"))

    def test_breadcrumbs_ficam_no_main_e_voltar_ao_lado_do_titulo(self):
        urls = (
            reverse("eleicao:painel_eleicoes"),
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,)),
            reverse("eleicao:painel_eleicao_nova"),
            f"{reverse('eleicao:painel_chapas')}?eleicao={self.eleicao.pk}",
            f"{reverse('eleicao:painel_chapa_nova')}?eleicao={self.eleicao.pk}",
            f"{reverse('eleicao:painel_importar')}?eleicao={self.eleicao.pk}",
            f"{reverse('eleicao:painel_eleitores')}?eleicao={self.eleicao.pk}",
            reverse("eleicao:painel_apuracao"),
            reverse("eleicao:painel_zeresima"),
        )
        for url in urls:
            with self.subTest(url=url):
                resposta = self.client.get(url)
                self.assertEqual(resposta.status_code, 200)
                html = resposta.content.decode()
                inicio_main = html.index("<main")
                inicio_breadcrumb = html.index('<nav aria-label="Breadcrumb"')
                fim_main = html.index("</main>", inicio_main)
                self.assertLess(inicio_main, inicio_breadcrumb)
                self.assertLess(inicio_breadcrumb, fim_main)

                inicio_area_titulo = html.index(
                    '<div class="flex min-w-0 flex-1 items-center gap-4">'
                )
                fim_cabecalho = html.index("</header>", inicio_area_titulo)
                area_cabecalho = html[inicio_area_titulo:fim_cabecalho]
                inicio_botao_voltar = area_cabecalho.index('aria-label="Voltar"')
                fim_botao_voltar = area_cabecalho.index("</a>", inicio_botao_voltar)
                inicio_coluna_texto = area_cabecalho.index(
                    '<div class="flex min-w-0 flex-col">'
                )
                fim_coluna_texto = area_cabecalho.index("</div>", inicio_coluna_texto)
                inicio_titulo = area_cabecalho.index("<h1", inicio_coluna_texto)
                inicio_descricao = area_cabecalho.index(
                    '<p class="mt-1 text-sm text-slate-500">', inicio_titulo
                )

                self.assertLess(fim_botao_voltar, inicio_coluna_texto)
                self.assertLess(inicio_coluna_texto, inicio_titulo)
                self.assertLess(inicio_titulo, inicio_descricao)
                self.assertLess(inicio_descricao, fim_coluna_texto)
                self.assertIn('title="Voltar"', area_cabecalho)
                self.assertIn("shrink-0", area_cabecalho[:inicio_coluna_texto])
                self.assertNotIn("←", area_cabecalho)
                self.assertNotIn(">Voltar<", area_cabecalho)
                for categoria in (
                    "Organização",
                    "Base eleitoral",
                    "Encerramento eleitoral",
                    "Documento eleitoral",
                ):
                    self.assertNotIn(categoria, area_cabecalho)

    def test_dashboard_alinha_texto_sem_botao_de_voltar(self):
        resposta = self.client.get(reverse("eleicao:painel"))
        html = resposta.content.decode()
        inicio_area_titulo = html.index(
            '<div class="flex min-w-0 flex-1 items-center gap-4">'
        )
        fim_cabecalho = html.index("</header>", inicio_area_titulo)
        area_cabecalho = html[inicio_area_titulo:fim_cabecalho]
        inicio_coluna_texto = area_cabecalho.index(
            '<div class="flex min-w-0 flex-col">'
        )
        fim_coluna_texto = area_cabecalho.index("</div>", inicio_coluna_texto)
        inicio_titulo = area_cabecalho.index("<h1", inicio_coluna_texto)
        inicio_descricao = area_cabecalho.index(
            '<p class="mt-1 text-sm text-slate-500">', inicio_titulo
        )

        self.assertNotIn('aria-label="Voltar"', area_cabecalho)
        self.assertLess(inicio_coluna_texto, inicio_titulo)
        self.assertLess(inicio_titulo, inicio_descricao)
        self.assertLess(inicio_descricao, fim_coluna_texto)
        self.assertNotIn("Resumo operacional", area_cabecalho)

    def test_dashboard_exibe_metricas_e_atalhos_sem_formularios(self):
        eleitor = self.criar_eleitor()
        eleitor.email_enviado = True
        eleitor.ja_votou = True
        eleitor.save(update_fields=("email_enviado", "ja_votou"))
        Voto.objects.create(
            eleicao=self.eleicao,
            chapa=self.chapa,
            tipo_voto=Voto.Tipo.VALIDO,
        )

        resposta = self.client.get(reverse("eleicao:painel"))

        self.assertContains(resposta, "Eleitores importados")
        self.assertContains(resposta, "Aceitos pelo SMTP")
        self.assertContains(resposta, "100,0%")
        self.assertContains(resposta, "Gerenciar eleições")
        self.assertContains(resposta, reverse("eleicao:painel_eleicoes"))
        self.assertContains(
            resposta,
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,)),
        )
        self.assertNotContains(resposta, f'href="{reverse("eleicao:painel_chapas")}"')
        self.assertNotContains(resposta, f'href="{reverse("eleicao:painel_eleitores")}"')
        self.assertNotContains(resposta, "Salvar eleição")
        self.assertNotContains(resposta, 'name="acao" value="chapa"')
        self.assertContains(resposta, 'id="sidebar-toggle"')
        self.assertContains(resposta, 'id="mobile-sidebar-toggle"')
        self.assertContains(resposta, 'aria-label="Abrir menu"')
        self.assertContains(resposta, 'id="mobile-sidebar-close"')
        self.assertContains(resposta, 'id="mobile-sidebar-backdrop"')
        self.assertContains(resposta, 'aria-label="Navegação do painel"')
        self.assertContains(resposta, "text-base font-semibold leading-tight text-slate-900")
        self.assertContains(resposta, "whitespace-nowrap text-xs text-slate-500")
        self.assertContains(resposta, "Sistema Eleitoral")
        self.assertContains(resposta, "e-Votos")
        self.assertContains(resposta, "px-4 py-3.5")
        self.assertContains(resposta, "p-1.5 rounded-md text-slate-400")
        self.assertContains(resposta, '<svg class="h-4 w-4 shrink-0"')
        self.assertContains(resposta, 'aria-label="Recolher menu"')
        self.assertContains(resposta, 'title="Recolher menu"')
        self.assertContains(resposta, "rotate(180deg)")
        self.assertContains(resposta, "flex-direction: column")
        self.assertNotContains(resposta, "data-sidebar-toggle-label")
        self.assertContains(resposta, "localStorage")

    def test_dashboard_esconde_indicadores_sem_eleicao_ativa(self):
        self.eleicao.ativa = False
        self.eleicao.save(update_fields=("ativa",))

        resposta = self.client.get(reverse("eleicao:painel"))

        self.assertContains(resposta, "Nenhuma eleição ativa")
        self.assertNotContains(resposta, "Eleitores importados")
        self.assertNotContains(resposta, "Votos registrados")

    def test_dashboard_mostra_metricas_somente_da_eleicao_selecionada(self):
        agora = timezone.now()
        outra_eleicao = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="Outra base eleitoral.",
            data_inicio=agora + timedelta(hours=4),
            data_fim=agora + timedelta(hours=5),
        )
        Eleitor.objects.create(
            eleicao=outra_eleicao,
            nome="Eleitor de outra eleição",
            email="outra@example.com",
            matricula="outra-001",
        )
        self.criar_eleitor()

        resposta = self.client.get(reverse("eleicao:painel"))

        self.assertEqual(resposta.context["eleicao"], self.eleicao)
        self.assertEqual(resposta.context["total_eleitores"], 1)

    def test_lista_de_eleitores_mantem_cada_base_separada_por_eleicao(self):
        self.criar_eleitor()
        agora = timezone.now()
        outra_eleicao = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="Outra base eleitoral.",
            data_inicio=agora + timedelta(hours=4),
            data_fim=agora + timedelta(hours=5),
        )
        Eleitor.objects.create(
            eleicao=outra_eleicao,
            nome="Eleitor de outra eleição",
            email="outra@example.com",
            matricula="outra-001",
        )

        resposta = self.client.get(
            reverse("eleicao:painel_eleitores"),
            {"eleicao": self.eleicao.pk},
        )

        self.assertContains(resposta, "Ana Silva")
        self.assertNotContains(resposta, "Eleitor de outra eleição")

    def test_detalhes_da_eleicao_preservam_dados_depois_do_encerramento(self):
        self.criar_eleitor()
        agora = timezone.now()
        Eleicao.objects.filter(pk=self.eleicao.pk).update(
            data_inicio=agora - timedelta(days=2),
            data_fim=agora - timedelta(days=1),
        )

        resposta = self.client.get(
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,))
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Eleição do DCE")
        self.assertContains(resposta, "Renova")
        self.assertContains(resposta, "1 eleitor")
        self.assertNotContains(resposta, "Importar eleitores para esta eleição")
        self.assertContains(resposta, "Ver chapas")
        self.assertContains(resposta, "Ver eleitores")
        self.assertNotContains(resposta, "Gerenciar chapas")
        self.assertNotContains(resposta, "Gerenciar eleitores")

    def test_eleicao_agendada_exibe_acoes_para_cadastrar_chapa_e_eleitores(self):
        resposta = self.client.get(
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,))
        )

        self.assertContains(resposta, "Cadastrar chapa")
        self.assertContains(resposta, "Adicionar eleitores")

        resposta = self.client.get(
            reverse("eleicao:painel_chapas"),
            {"eleicao": self.eleicao.pk},
        )
        self.assertContains(resposta, "Cadastrar chapa")

        resposta = self.client.get(
            reverse("eleicao:painel_eleitores"),
            {"eleicao": self.eleicao.pk},
        )
        self.assertContains(resposta, "Adicionar eleitores")

    def test_eleicao_aberta_exibe_aviso_e_bloqueia_novos_cadastros(self):
        agora = timezone.now()
        self.eleicao.data_inicio = agora - timedelta(minutes=1)
        self.eleicao.data_fim = agora + timedelta(hours=1)
        self.eleicao.save(update_fields=("data_inicio", "data_fim"))

        resposta = self.client.get(
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,))
        )

        self.assertContains(resposta, "A eleição já foi iniciada.")
        self.assertNotContains(resposta, "Cadastrar chapa")
        self.assertNotContains(resposta, "Adicionar eleitores")

    def test_eleicoes_encerradas_tem_acao_ver_e_ativas_acao_gerenciar(self):
        agora = timezone.now()
        encerrada = Eleicao.objects.create(
            titulo="Eleição encerrada",
            descricao="Histórico",
            data_inicio=agora - timedelta(days=2),
            data_fim=agora - timedelta(days=1),
        )

        resposta = self.client.get(reverse("eleicao:painel_eleicoes"))

        self.assertContains(resposta, "Gerenciar")
        self.assertContains(resposta, "Eleição encerrada")
        self.assertContains(resposta, "Ver")
        self.assertContains(
            resposta,
            f'href="{reverse("eleicao:painel_eleicao_detalhes", args=(encerrada.pk,))}"',
        )
        self.assertContains(resposta, 'd="M5 12h14m-7-7 7 7-7 7"')

    def test_lista_de_eleicoes_destaca_uma_aberta_e_separa_agendadas_e_encerradas(self):
        agora = timezone.now()
        Eleicao.objects.filter(pk=self.eleicao.pk).update(
            data_inicio=agora - timedelta(hours=3),
            data_fim=agora + timedelta(hours=1),
        )
        outra_aberta = Eleicao.objects.create(
            titulo="Eleição em destaque",
            descricao="Votação atual.",
            data_inicio=agora - timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        agendada = Eleicao.objects.create(
            titulo="Próxima eleição",
            descricao="Votação futura.",
            data_inicio=agora + timedelta(days=1),
            data_fim=agora + timedelta(days=2),
        )
        encerrada = Eleicao.objects.create(
            titulo="Eleições encerradas antigas",
            descricao="Histórico.",
            data_inicio=agora - timedelta(days=2),
            data_fim=agora - timedelta(days=1),
        )

        resposta = self.client.get(reverse("eleicao:painel_eleicoes"))

        self.assertEqual(resposta.context["eleicao_destaque"], outra_aberta)
        self.assertEqual(list(resposta.context["outras_abertas"]), [self.eleicao])
        self.assertEqual(list(resposta.context["eleicoes_agendadas"]), [agendada])
        self.assertEqual(list(resposta.context["eleicoes_encerradas"]), [encerrada])
        self.assertContains(resposta, "Eleição ativa")
        self.assertContains(resposta, "Eleições agendadas")
        self.assertContains(resposta, "Eleições encerradas")

    def test_cria_eleicao_na_tela_dedicada(self):
        agora = timezone.localtime(timezone.now())
        inicio = (agora + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        fim = (agora + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        resposta = self.client.post(
            reverse("eleicao:painel_eleicao_nova"),
            {
                "titulo": "Eleição extraordinária",
                "descricao": "Nova votação",
                "membros_comissao": "  Maria Silva  \n\nJoão Souza ",
                "data_inicio": inicio,
                "data_fim": fim,
                "ativa": "on",
            },
        )
        nova_eleicao = Eleicao.objects.get(titulo="Eleição extraordinária")
        self.assertRedirects(
            resposta,
            reverse("eleicao:painel_eleicao_detalhes", args=(nova_eleicao.pk,)),
        )
        self.assertEqual(nova_eleicao.membros_comissao, "Maria Silva\nJoão Souza")

    def test_edita_eleicao_e_chapa_em_paginas_dedicadas(self):
        agora = timezone.localtime(timezone.now())
        resposta = self.client.post(
            reverse("eleicao:painel_chapa_editar", args=(self.chapa.pk,)),
            {
                "eleicao": self.eleicao.pk,
                "numero": 2,
                "nome": "Renova Agora",
                "slogan": "Novo slogan",
            },
        )
        self.assertRedirects(resposta, reverse("eleicao:painel_chapas"))
        self.chapa.refresh_from_db()
        self.assertEqual(self.chapa.numero, 2)
        self.assertEqual(self.chapa.nome, "Renova Agora")
        resposta = self.client.post(
            reverse("eleicao:painel_eleicao_editar", args=(self.eleicao.pk,)),
            {
                "titulo": "Eleição atualizada",
                "descricao": self.eleicao.descricao,
                "data_inicio": (agora - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                "data_fim": (agora + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                "ativa": "on",
            },
        )
        self.assertRedirects(
            resposta,
            reverse("eleicao:painel_eleicao_detalhes", args=(self.eleicao.pk,)),
        )
        self.eleicao.refresh_from_db()
        self.assertEqual(self.eleicao.titulo, "Eleição atualizada")

    @staticmethod
    def criar_imagem_upload(formato, extensao):
        conteudo = BytesIO()
        Image.new("RGB", (2, 2), color="red").save(conteudo, format=formato)
        return SimpleUploadedFile(
            f"chapa{extensao}",
            conteudo.getvalue(),
            content_type=f"image/{'jpeg' if formato == 'JPEG' else formato.lower()}",
        )

    def test_chapa_aceita_apenas_imagens_validas_png_jpg_webp_de_ate_2_mb(self):
        for indice, (formato, extensao) in enumerate(
            (("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")),
            start=1,
        ):
            with self.subTest(formato=formato):
                formulario = ChapaForm(
                    data={
                        "eleicao": self.eleicao.pk,
                        "numero": indice + 1,
                        "nome": f"Chapa {indice}",
                    },
                    files={"foto": self.criar_imagem_upload(formato, extensao)},
                )
                self.assertTrue(formulario.is_valid(), formulario.errors)

        arquivo_invalido = SimpleUploadedFile(
            "malicioso.png",
            b"nao e uma imagem",
            content_type="image/png",
        )
        formulario_invalido = ChapaForm(
            data={"eleicao": self.eleicao.pk, "numero": 4, "nome": "Invalida"},
            files={"foto": arquivo_invalido},
        )
        self.assertFalse(formulario_invalido.is_valid())
        self.assertIn("foto", formulario_invalido.errors)

        arquivo_grande = SimpleUploadedFile(
            "grande.png",
            self.criar_imagem_upload("PNG", ".png").read()
            + b"x" * (2 * 1024 * 1024 + 1),
            content_type="image/png",
        )
        formulario_grande = ChapaForm(
            data={"eleicao": self.eleicao.pk, "numero": 6, "nome": "Grande"},
            files={"foto": arquivo_grande},
        )
        self.assertFalse(formulario_grande.is_valid())
        self.assertIn("2 MB", str(formulario_grande.errors["foto"]))

    def test_importacao_rejeita_valores_com_prefixos_de_formula(self):
        prefixos_perigosos = ("=", "+", "-", "@", "\t", "\r")
        for prefixo in prefixos_perigosos:
            with self.subTest(prefixo=repr(prefixo)):
                with self.assertRaisesRegex(ValueError, "caractere reservado"):
                    _normalizar_linhas(
                        [
                            ["nome", "email", "matricula"],
                            [f"{prefixo}1+1", "pessoa@example.com", "001"],
                        ]
                    )

        with patch("eleicao.painel_views.call_command") as disparar:
            resposta = self.client.post(
                reverse("eleicao:painel_importar"),
                {
                    "origem": "texto",
                    "texto-eleicao": self.eleicao.pk,
                    "texto-texto": "=1+1,pessoa@example.com,009",
                },
            )

        self.assertContains(
            resposta,
            "caractere reservado para fórmulas",
            status_code=400,
        )
        self.assertFalse(Eleitor.objects.filter(email="pessoa@example.com").exists())
        disparar.assert_not_called()

    def test_cria_chapa_e_filtra_lista_por_eleicao(self):
        resposta = self.client.post(
            reverse("eleicao:painel_chapa_nova"),
            {
                "eleicao": self.eleicao.pk,
                "numero": 7,
                "nome": "Horizonte",
                "slogan": "Uma universidade para todos",
            },
        )
        self.assertRedirects(resposta, reverse("eleicao:painel_chapas"))
        self.assertTrue(Chapa.objects.filter(eleicao=self.eleicao, numero=7).exists())

        outra_eleicao = Eleicao.objects.create(
            titulo="Eleição alternativa",
            descricao="Outra votação",
            data_inicio=self.eleicao.data_inicio,
            data_fim=self.eleicao.data_fim,
        )
        Chapa.objects.create(eleicao=outra_eleicao, numero=3, nome="Outra chapa")
        resposta = self.client.get(
            reverse("eleicao:painel_chapas"),
            {"eleicao": self.eleicao.pk},
        )
        self.assertContains(resposta, "Renova")
        self.assertNotContains(resposta, "Outra chapa")

    def iniciar_eleicao(self):
        self.eleicao.data_inicio = timezone.now() - timedelta(minutes=1)
        self.eleicao.save(update_fields=("data_inicio",))

    def test_bloqueia_edicao_e_exclusao_de_eleicao_e_chapa_apos_inicio(self):
        self.iniciar_eleicao()

        self.eleicao.titulo = "Título alterado"
        with self.assertRaises(ValidationError):
            self.eleicao.save()
        with self.assertRaises(ValidationError):
            Eleicao.objects.filter(pk=self.eleicao.pk).update(titulo="Título em lote")
        with self.assertRaises(ValidationError):
            self.eleicao.delete()

        self.chapa.nome = "Chapa alterada"
        with self.assertRaises(ValidationError):
            self.chapa.save()
        with self.assertRaises(ValidationError):
            Chapa.objects.filter(pk=self.chapa.pk).delete()

        resposta = self.client.post(
            reverse("eleicao:painel_eleicao_editar", args=(self.eleicao.pk,)),
            {
                "titulo": "Título via painel",
                "descricao": self.eleicao.descricao,
                "data_inicio": timezone.localtime(self.eleicao.data_inicio).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "data_fim": timezone.localtime(self.eleicao.data_fim).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "ativa": "on",
            },
        )
        self.assertRedirects(resposta, reverse("eleicao:painel_eleicoes"))
        self.eleicao.refresh_from_db()
        self.assertEqual(self.eleicao.titulo, "Eleição do DCE")

        resposta = self.client.post(
            reverse("eleicao:painel_chapa_editar", args=(self.chapa.pk,)),
            {
                "eleicao": self.eleicao.pk,
                "numero": 9,
                "nome": "Chapa via painel",
            },
        )
        self.assertRedirects(resposta, reverse("eleicao:painel_chapas"))
        self.chapa.refresh_from_db()
        self.assertEqual(self.chapa.nome, "Renova")

    def test_bloqueia_matricula_e_exclusao_e_permite_email_para_nao_votante(self):
        eleitor = self.criar_eleitor()
        self.iniciar_eleicao()

        eleitor.matricula = "999"
        with self.assertRaises(ValidationError):
            eleitor.save()
        eleitor.refresh_from_db()
        self.assertEqual(eleitor.matricula, "001")

        eleitor.email = "ana.nova@example.com"
        eleitor.save(update_fields=("email",))
        eleitor.refresh_from_db()
        self.assertEqual(eleitor.email, "ana.nova@example.com")

        with self.assertRaises(ValidationError):
            eleitor.delete()
        with self.assertRaises(ValidationError):
            Eleitor.objects.filter(pk=eleitor.pk).delete()

        eleitor.ja_votou = True
        eleitor.save(update_fields=("ja_votou",))
        eleitor.email = "nao-alterar@example.com"
        with self.assertRaises(ValidationError):
            eleitor.save(update_fields=("email",))
        with self.assertRaises(ValidationError):
            Eleitor.objects.filter(pk=eleitor.pk).update(ja_votou=False)

    def test_painel_bloqueia_edicao_de_email_e_reenvio_apos_voto(self):
        eleitor = self.criar_eleitor()
        self.iniciar_eleicao()
        eleitor.ja_votou = True
        eleitor.save(update_fields=("ja_votou",))
        url = reverse("eleicao:painel_eleitores")

        resposta = self.client.post(
            url,
            {
                "acao": "editar_email",
                "eleitor_id": eleitor.pk,
                "email": "novo@example.com",
            },
            follow=True,
        )
        self.assertContains(resposta, "não pode ser alterado após o eleitor votar")
        eleitor.refresh_from_db()
        self.assertEqual(eleitor.email, "ana@example.com")

        with patch("eleicao.painel_views.enviar_convite") as enviar:
            resposta = self.client.post(
                url,
                {"acao": "reenviar", "eleitor_id": eleitor.pk},
                follow=True,
            )
        self.assertContains(resposta, "Não é possível reenviar o convite após o eleitor votar")
        enviar.assert_not_called()

    def test_zeresima_imprimivel_mostra_eleitores_chapas_e_assinaturas(self):
        self.criar_eleitor()
        self.eleicao.membros_comissao = " Maria Silva \n\n João Souza "
        self.eleicao.save(update_fields=("membros_comissao",))

        resposta = self.client.get(
            reverse("eleicao:painel_zeresima"),
            {"eleicao": self.eleicao.pk},
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "painel/zeresima.html")
        self.assertContains(resposta, "Zerésima da Urna")
        self.assertContains(resposta, "Comissão Eleitoral")
        self.assertContains(resposta, self.eleicao.titulo)
        self.assertContains(resposta, "Período oficial")
        self.assertContains(resposta, "Status")
        self.assertContains(resposta, "Emitido em")
        self.assertContains(resposta, "Resumo / Totalização")
        self.assertContains(resposta, "Eleitores aptos")
        self.assertContains(resposta, "1")
        self.assertContains(resposta, "Renova")
        self.assertContains(resposta, "Percentual")
        self.assertContains(resposta, "0,00%")
        self.assertContains(resposta, "Conferência de Integridade")
        self.assertContains(resposta, "Membros da Comissão Eleitoral")
        self.assertContains(resposta, "Maria Silva — assinatura")
        self.assertContains(resposta, "João Souza — assinatura")
        self.assertNotContains(resposta, "Fiscais das chapas")
        self.assertNotContains(resposta, "Atenção: a urna já foi iniciada.")

    def test_zeresima_alerta_se_houver_votos(self):
        Voto.objects.create(
            eleicao=self.eleicao,
            chapa=self.chapa,
            tipo_voto=Voto.Tipo.VALIDO,
        )

        resposta = self.client.get(
            reverse("eleicao:painel_zeresima"),
            {"eleicao": self.eleicao.pk},
        )

        self.assertContains(resposta, "Atenção: a urna já foi iniciada.")
        self.assertContains(resposta, "Há 1 voto(s) registrado(s)")

    def test_zeresima_omite_eleicoes_encerradas_e_rejeita_selecao_direta(self):
        agora = timezone.now()
        eleicao_encerrada = Eleicao.objects.create(
            titulo="Eleição encerrada",
            descricao="Não deve aparecer na zerésima.",
            data_inicio=agora - timedelta(hours=2),
            data_fim=agora - timedelta(hours=1),
        )

        resposta = self.client.get(reverse("eleicao:painel_zeresima"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, self.eleicao.titulo)
        self.assertNotContains(resposta, eleicao_encerrada.titulo)

        resposta = self.client.get(
            reverse("eleicao:painel_zeresima"),
            {"eleicao": eleicao_encerrada.pk},
        )
        self.assertEqual(resposta.status_code, 404)

    def test_zeresima_exige_usuario_staff(self):
        self.client.logout()

        resposta = self.client.get(reverse("eleicao:painel_zeresima"))

        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.url.startswith(settings.LOGIN_URL))

    @patch("eleicao.painel_views.call_command")
    def test_importa_csv_ignora_duplicados_e_reporta_invalidos(self, disparar):
        conteudo = (
            "nome,email,matricula\n"
            "Ana Silva,ana@example.com,001\n"
            "Ana Duplicada,ANA@example.com,002\n"
            "Nome Inválido,nao-e-email,003\n"
        ).encode()
        arquivo = SimpleUploadedFile("eleitores.csv", conteudo, content_type="text/csv")

        resposta = self.client.post(
            reverse("eleicao:painel_importar"),
            {
                "origem": "arquivo",
                "arquivo-eleicao": self.eleicao.pk,
                "arquivo-arquivo": arquivo,
            },
            follow=True,
        )

        self.assertEqual(Eleitor.objects.filter(eleicao=self.eleicao).count(), 1)
        self.assertContains(resposta, "1 importados, 1 duplicados ignorados e 1 inválidos")

    def test_importacao_dispara_convites_automaticamente(self):
        def simular_disparo(*args, **kwargs):
            Eleitor.objects.filter(
                eleicao=self.eleicao,
                email_enviado=False,
                ja_votou=False,
            ).update(email_enviado=True)

        with patch(
            "eleicao.painel_views.call_command",
            side_effect=simular_disparo,
        ) as disparar:
            resposta = self.client.post(
                reverse("eleicao:painel_importar"),
                {
                    "origem": "texto",
                    "texto-eleicao": self.eleicao.pk,
                    "texto-texto": "nome,email,matricula\nAna,ana@example.com,001",
                },
                follow=True,
            )

        self.assertContains(resposta, "Os 1 convites pendentes foram aceitos pelo SMTP")
        disparar.assert_called_once()
        self.assertEqual(disparar.call_args.args[0], "disparar_convites")
        self.assertEqual(disparar.call_args.kwargs["eleicao"], self.eleicao.pk)
        self.assertEqual(disparar.call_args.kwargs["limite"], 1)

    def test_importacao_contextual_nao_permite_trocar_a_eleicao(self):
        agora = timezone.now()
        outra_eleicao = Eleicao.objects.create(
            titulo="Outra eleição",
            descricao="Cadastro separado.",
            data_inicio=agora + timedelta(days=2),
            data_fim=agora + timedelta(days=3),
        )

        resposta = self.client.post(
            f"{reverse('eleicao:painel_importar')}?eleicao={self.eleicao.pk}",
            {
                "origem": "texto",
                "texto-eleicao": outra_eleicao.pk,
                "texto-texto": "nome,email,matricula\nPessoa,pessoa@example.com,999",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(Eleitor.objects.filter(email="pessoa@example.com").exists())

    @patch("eleicao.painel_views.call_command")
    def test_importa_planilha_xlsx(self, disparar):
        planilha = Workbook()
        folha = planilha.active
        folha.append(["nome", "email", "matricula"])
        folha.append(["Bruno Souza", "bruno@example.com", "002"])
        conteudo = BytesIO()
        planilha.save(conteudo)
        arquivo = SimpleUploadedFile(
            "eleitores.xlsx",
            conteudo.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        resposta = self.client.post(
            reverse("eleicao:painel_importar"),
            {
                "origem": "arquivo",
                "arquivo-eleicao": self.eleicao.pk,
                "arquivo-arquivo": arquivo,
            },
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(Eleitor.objects.filter(email="bruno@example.com").exists())

    @patch("eleicao.painel_views.call_command")
    def test_importa_texto_colado_com_delimitadores_comuns(self, disparar):
        tela_importacao = self.client.get(reverse("eleicao:painel_importar"))
        self.assertContains(tela_importacao, "cabeçalho é opcional")
        self.assertContains(tela_importacao, "ponto-e-vírgula ou tabulação")
        self.assertContains(tela_importacao, "001234")
        detalhes = re.findall(
            r"<details\b[^>]*>",
            tela_importacao.content.decode(),
        )
        self.assertEqual(len(detalhes), 3)
        self.assertTrue(all(" open" not in detalhe for detalhe in detalhes))
        self.assertEqual(tela_importacao.content.count(b'class="seta '), 3)

        delimitadores = (",", ";", "\t")
        for indice, delimitador in enumerate(delimitadores, start=1):
            with self.subTest(delimitador=repr(delimitador)):
                texto = (
                    f" NOME {delimitador} EMAIL {delimitador} MATRICULA\n"
                    f" Eleitor {indice} {delimitador} Pessoa{indice}@Example.com "
                    f"{delimitador} 00{indice} \n"
                )
                resposta = self.client.post(
                    reverse("eleicao:painel_importar"),
                    {
                        "origem": "texto",
                        "texto-eleicao": self.eleicao.pk,
                        "texto-texto": texto,
                    },
                    follow=True,
                )
                self.assertContains(resposta, "1 importados")
                eleitor = Eleitor.objects.get(matricula=f"00{indice}")
                self.assertEqual(eleitor.email, f"pessoa{indice}@example.com")

    @patch("eleicao.painel_views.call_command")
    def test_importa_dados_colados_sem_cabecalho(self, disparar):
        delimitadores = (",", ";", "\t")
        for indice, delimitador in enumerate(delimitadores, start=1):
            with self.subTest(delimitador=repr(delimitador)):
                texto = (
                    f"\nPessoa {indice}{delimitador}"
                    f"pessoa{indice}@example.com{delimitador}00{indice}\n"
                )
                resposta = self.client.post(
                    reverse("eleicao:painel_importar"),
                    {
                        "origem": "texto",
                        "texto-eleicao": self.eleicao.pk,
                        "texto-texto": texto,
                    },
                    follow=True,
                )

                self.assertContains(resposta, "1 importados")
                eleitor = Eleitor.objects.get(matricula=f"00{indice}")
                self.assertEqual(eleitor.nome, f"Pessoa {indice}")
                self.assertEqual(eleitor.email, f"pessoa{indice}@example.com")

    @patch("eleicao.painel_views.call_command")
    def test_importa_cabecalho_colado_com_crases_e_bom(self, disparar):
        texto = "\ufeff`nome`,`email`,`matricula`\nAna Silva,ana@example.com,001234"

        resposta = self.client.post(
            reverse("eleicao:painel_importar"),
            {
                "origem": "texto",
                "texto-eleicao": self.eleicao.pk,
                "texto-texto": texto,
            },
            follow=True,
        )

        self.assertContains(resposta, "1 importados")
        self.assertTrue(Eleitor.objects.filter(matricula="001234").exists())

    def test_download_do_csv_modelo_exige_staff_e_tem_cabecalhos(self):
        resposta = self.client.get(reverse("eleicao:painel_modelo_csv"))

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("attachment;", resposta["Content-Disposition"])
        self.assertIn("nome,email,matricula", resposta.content.decode("utf-8-sig"))

    def test_importacao_rejeita_colunas_ausentes(self):
        arquivo = SimpleUploadedFile(
            "eleitores.csv",
            b"nome,email\nAna,ana@example.com\n",
            content_type="text/csv",
        )

        resposta = self.client.post(
            reverse("eleicao:painel_importar"),
            {
                "origem": "arquivo",
                "arquivo-eleicao": self.eleicao.pk,
                "arquivo-arquivo": arquivo,
            },
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertContains(resposta, "Cabeçalho inválido", status_code=400)
        self.assertEqual(Eleitor.objects.count(), 0)

    def test_busca_filtro_edicao_de_email_e_reenvio_imediato(self):
        eleitor = self.criar_eleitor()
        self.criar_eleitor(
            nome="Carlos Souza", email="carlos@example.com", matricula="002"
        )
        url = reverse("eleicao:painel_eleitores")

        resposta = self.client.get(
            url,
            {
                "eleicao": self.eleicao.pk,
                "q": "ana@",
                "status": "pendentes",
            },
        )

        self.assertContains(resposta, "Ana Silva")
        self.assertNotContains(resposta, "Carlos Souza")
        resposta = self.client.post(
            url,
            {
                "acao": "editar_email",
                "eleitor_id": eleitor.pk,
                "eleicao": self.eleicao.pk,
                "email": "ana.nova@example.com",
                "q": "ana@",
                "status": "pendentes",
            },
        )
        self.assertRedirects(
            resposta,
            f"{url}?eleicao={self.eleicao.pk}&q=ana%40&status=pendentes",
        )
        eleitor.refresh_from_db()
        self.assertEqual(eleitor.email, "ana.nova@example.com")
        self.assertFalse(eleitor.email_enviado)

        with patch("eleicao.painel_views.enviar_convite", return_value=1) as enviar:
            resposta = self.client.post(
                url,
                {"acao": "reenviar", "eleitor_id": eleitor.pk},
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "O servidor SMTP aceitou o convite")
        enviar.assert_called_once_with(eleitor, settings.SITE_BASE_URL)
        eleitor.refresh_from_db()
        self.assertTrue(eleitor.email_enviado)
        self.assertIsNotNone(eleitor.data_envio)
        self.assertEqual(eleitor.erro_envio, "")

    def test_lista_permite_disparo_manual_dos_convites_pendentes(self):
        self.criar_eleitor()
        self.criar_eleitor(
            nome="Carlos Souza", email="carlos@example.com", matricula="002"
        )
        url = reverse("eleicao:painel_eleitores")
        resposta = self.client.get(url, {"eleicao": self.eleicao.pk})

        self.assertContains(resposta, "2 pendentes")
        self.assertContains(resposta, "Disparar convites pendentes")

        def simular_disparo(*args, **kwargs):
            Eleitor.objects.filter(
                eleicao=self.eleicao,
                email_enviado=False,
                ja_votou=False,
            ).update(email_enviado=True)

        with patch(
            "eleicao.painel_views.call_command",
            side_effect=simular_disparo,
        ) as disparar:
            resposta = self.client.post(
                url,
                {
                    "acao": "enviar_pendentes",
                    "eleicao": self.eleicao.pk,
                    "q": "",
                    "status": "",
                },
                follow=True,
            )

        self.assertContains(resposta, "Todos os 2 convites pendentes")
        disparar.assert_called_once()
        self.assertEqual(disparar.call_args.args[0], "disparar_convites")
        self.assertEqual(disparar.call_args.kwargs["eleicao"], self.eleicao.pk)
        self.assertEqual(disparar.call_args.kwargs["limite"], 2)

    def test_edicao_email_invalido_e_erro_de_reenvio_sao_mostrados(self):
        eleitor = self.criar_eleitor()
        url = reverse("eleicao:painel_eleitores")
        self.client.post(
            url,
            {"acao": "editar_email", "eleitor_id": eleitor.pk, "email": "invalido"},
            follow=True,
        )
        eleitor.refresh_from_db()
        self.assertEqual(eleitor.email, "ana@example.com")

        with patch(
            "eleicao.painel_views.enviar_convite",
            side_effect=SMTPDataError(550, b"destinatario indisponivel"),
        ):
            resposta = self.client.post(
                url,
                {"acao": "reenviar", "eleitor_id": eleitor.pk},
                follow=True,
            )
        self.assertContains(resposta, "550")
        eleitor.refresh_from_db()
        self.assertIn("550", eleitor.erro_envio)
        self.assertFalse(eleitor.email_enviado)

    def test_apuracao_nao_exibe_resultado_antes_do_encerramento(self):
        self.eleicao.data_inicio = timezone.now() - timedelta(minutes=1)
        self.eleicao.save(update_fields=("data_inicio",))
        url = reverse("eleicao:painel_apuracao")

        resposta = self.client.get(url, {"eleicao": self.eleicao.pk})

        self.assertContains(resposta, "A apuração será liberada")
        self.assertNotContains(resposta, "Votos por chapa")
        self.assertEqual(
            self.client.get(
                reverse("eleicao:painel_comparecimento_csv"),
                {"eleicao": self.eleicao.pk},
            ).status_code,
            404,
        )

    def test_pesquisa_apuracao_mostra_eleicoes_atuais_e_ultima_encerrada(self):
        agora = timezone.now()
        self.eleicao.data_inicio = agora - timedelta(minutes=1)
        self.eleicao.data_fim = agora + timedelta(hours=1)
        self.eleicao.save(update_fields=("data_inicio", "data_fim"))
        antiga = Eleicao.objects.create(
            titulo="Eleição histórica antiga",
            descricao="",
            data_inicio=agora - timedelta(days=10),
            data_fim=agora - timedelta(days=9),
        )
        ultima_encerrada = Eleicao.objects.create(
            titulo="Eleição encerrada mais recente",
            descricao="",
            data_inicio=agora - timedelta(days=2),
            data_fim=agora - timedelta(days=1),
        )
        Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="",
            data_inicio=agora + timedelta(days=1),
            data_fim=agora + timedelta(days=2),
        )

        resposta = self.client.get(reverse("eleicao:painel_apuracao"))

        self.assertContains(resposta, self.eleicao.titulo)
        self.assertContains(resposta, ultima_encerrada.titulo)
        self.assertNotContains(resposta, "Eleição futura")
        html = resposta.content.decode()
        opcoes_principais = re.search(
            r'<select id="apuracao-eleicao-atual"[^>]*>(.*?)</select>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(opcoes_principais)
        self.assertNotIn(antiga.titulo, opcoes_principais.group(1))
        self.assertContains(resposta, antiga.titulo)

        resposta_historica = self.client.get(
            reverse("eleicao:painel_apuracao"), {"eleicao": antiga.pk}
        )
        self.assertEqual(resposta_historica.status_code, 200)
        self.assertContains(resposta_historica, antiga.titulo)
        self.assertRegex(resposta_historica.content.decode(), r"<details [^>]*open>")
        self.assertEqual(
            self.client.get(
                reverse("eleicao:painel_comparecimento_csv"),
                {"eleicao": antiga.pk},
            ).status_code,
            200,
        )
        self.assertContains(
            self.client.get(
                reverse("eleicao:painel_apuracao"),
                {"eleicao": ultima_encerrada.pk},
            ),
            "Boletim de urna",
        )

    def encerrar_eleicao(self):
        self.eleicao.data_inicio = timezone.now() - timedelta(hours=2)
        self.eleicao.data_fim = timezone.now() - timedelta(minutes=1)
        self.eleicao.save(update_fields=("data_inicio", "data_fim"))

    def test_apuracao_valida_integridade_e_exibe_boletim(self):
        segunda_chapa = Chapa.objects.create(
            eleicao=self.eleicao,
            numero=2,
            nome="Avançar",
        )
        eleitor1 = self.criar_eleitor()
        eleitor2 = self.criar_eleitor(
            nome="Bruno Souza", email="bruno@example.com", matricula="002"
        )
        self.criar_eleitor(
            nome="Carla Nunes", email="carla@example.com", matricula="003"
        )
        eleitor1.nome = "=Ana Silva"
        eleitor1.save(update_fields=("nome",))
        eleitor1.ja_votou = True
        eleitor2.ja_votou = True
        eleitor1.save(update_fields=("ja_votou",))
        eleitor2.save(update_fields=("ja_votou",))
        voto_valido = Voto.objects.create(
            eleicao=self.eleicao, chapa=self.chapa, tipo_voto=Voto.Tipo.VALIDO
        )
        voto_branco = Voto.objects.create(
            eleicao=self.eleicao, tipo_voto=Voto.Tipo.BRANCO
        )
        self.encerrar_eleicao()

        resposta = self.client.get(
            reverse("eleicao:painel_apuracao"), {"eleicao": self.eleicao.pk}
        )

        self.assertContains(resposta, "Comissão Eleitoral")
        self.assertContains(resposta, "Período oficial")
        self.assertContains(resposta, "Emitido em")
        self.assertContains(resposta, "Resumo / Totalização")
        self.assertContains(resposta, "Total de Eleitores Aptos")
        self.assertContains(resposta, "Comparecimento")
        self.assertContains(resposta, "66,67%")
        self.assertContains(resposta, "Abstenções")
        self.assertContains(resposta, "33,33%")
        self.assertContains(resposta, "Total de Votos Válidos")
        self.assertContains(resposta, "Votos Brancos")
        self.assertContains(resposta, "Votos Nulos")
        self.assertContains(resposta, "Integridade validada")
        self.assertContains(resposta, "Integridade validada")
        self.assertContains(resposta, "Total de Votos == Total de Votantes")
        self.assertContains(resposta, "Percentual")
        self.assertContains(resposta, "100,00%")
        self.assertContains(resposta, "Vencedora")
        self.assertContains(resposta, "Quadro de Assinaturas")
        self.assertContains(resposta, "Assinatura digital do Boletim de Urna (SHA-256)")
        self.assertContains(resposta, ".print-receipts, .print-signatures")
        self.assertContains(resposta, "print:gap-1 print:text-[10px]")
        self.assertNotContains(resposta, "Fiscal")
        self.assertContains(resposta, "1")
        self.assertContains(resposta, voto_valido.codigo_comprovante)
        self.assertContains(resposta, voto_branco.codigo_comprovante)
        self.assertEqual(resposta.context["total_eleitores_aptos"], 3)
        self.assertEqual(resposta.context["comparecimento"], 2)
        self.assertEqual(resposta.context["abstencoes"], 1)
        self.assertEqual(resposta.context["total_votos_validos"], 1)
        self.assertAlmostEqual(resposta.context["percentual_comparecimento"], 200 / 3)
        self.assertAlmostEqual(resposta.context["percentual_abstencoes"], 100 / 3)
        self.assertEqual(
            resposta.content.count(b'class="h-8 border-b border-slate-400"'),
            3,
        )

        payload_assinatura = {
            "eleicao_id": self.eleicao.pk,
            "votos_por_chapa": [
                {"numero": self.chapa.numero, "total_votos": 1},
                {"numero": segunda_chapa.numero, "total_votos": 0},
            ],
            "comprovantes": sorted(
                (voto_valido.codigo_comprovante, voto_branco.codigo_comprovante)
            ),
        }
        assinatura_esperada = hashlib.sha256(
            json.dumps(
                payload_assinatura,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(resposta.context["assinatura_digital"], assinatura_esperada)
        self.assertContains(resposta, assinatura_esperada)
        resposta_repetida = self.client.get(
            reverse("eleicao:painel_apuracao"), {"eleicao": self.eleicao.pk}
        )
        self.assertEqual(
            resposta_repetida.context["assinatura_digital"], assinatura_esperada
        )

        csv_resposta = self.client.get(
            reverse("eleicao:painel_comparecimento_csv"),
            {"eleicao": self.eleicao.pk},
        )
        conteudo = csv_resposta.content.decode("utf-8-sig")
        linhas = list(csv.reader(StringIO(conteudo)))
        self.assertEqual(linhas[0], ["Nome", "Matrícula"])
        self.assertEqual(
            {linha[0] for linha in linhas[1:]}, {"'=Ana Silva", "Bruno Souza"}
        )
        self.assertNotIn(voto_valido.codigo_comprovante, conteudo)

    def test_csv_comparecimento_escapa_prefixos_de_formula_e_controle(self):
        prefixos_perigosos = ("=", "+", "-", "@", "\t", "\r")
        esperados = set()
        for indice, prefixo in enumerate(prefixos_perigosos, start=1):
            nome = f"{prefixo}Eleitor {indice}"
            matricula = f"{prefixo}00{indice}"
            Eleitor.objects.create(
                eleicao=self.eleicao,
                nome=nome,
                email=f"eleitor{indice}@example.com",
                matricula=matricula,
                ja_votou=True,
            )
            esperados.add((f"'{nome}", f"'{matricula}"))

        self.encerrar_eleicao()
        resposta = self.client.get(
            reverse("eleicao:painel_comparecimento_csv"),
            {"eleicao": self.eleicao.pk},
        )

        linhas = list(csv.reader(StringIO(resposta.content.decode("utf-8-sig"))))
        self.assertEqual(linhas[0], ["Nome", "Matrícula"])
        self.assertEqual(set(map(tuple, linhas[1:])), esperados)

    def test_apuracao_oculta_resultado_quando_comparecimento_diverge(self):
        eleitor = self.criar_eleitor()
        eleitor.ja_votou = True
        eleitor.save(update_fields=("ja_votou",))
        Voto.objects.create(
            eleicao=self.eleicao, chapa=self.chapa, tipo_voto=Voto.Tipo.VALIDO
        )
        Voto.objects.create(eleicao=self.eleicao, tipo_voto=Voto.Tipo.BRANCO)
        self.encerrar_eleicao()

        resposta = self.client.get(
            reverse("eleicao:painel_apuracao"), {"eleicao": self.eleicao.pk}
        )

        self.assertContains(resposta, "Divergência detectada")
        self.assertNotContains(resposta, "Votos por chapa")
        self.assertNotContains(resposta, "Comprovantes registrados")
