from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Chapa, Eleicao, Eleitor, Voto


class FluxoVotacaoTests(TestCase):
    def setUp(self):
        agora = timezone.now()
        self.eleicao = Eleicao.objects.create(
            titulo="Eleição do DCE",
            descricao="Eleição para o diretório central.",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        self.eleitor = Eleitor.objects.create(
            eleicao=self.eleicao,
            nome="João da Silva",
            email="joao@example.com",
            matricula="001234",
        )
        self.chapa = Chapa.objects.create(eleicao=self.eleicao, numero=1, nome="Renova")
        self.eleicao.data_inicio = agora - timedelta(hours=1)
        self.eleicao.save(update_fields=("data_inicio",))

    def criar_eleicao_encerrada_para_auditoria(self):
        agora = timezone.now()
        eleicao = Eleicao.objects.create(
            titulo="Eleição auditada",
            descricao="",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        chapas = [
            Chapa.objects.create(eleicao=eleicao, numero=2, nome="Chapa Dois"),
            Chapa.objects.create(eleicao=eleicao, numero=1, nome="Chapa Um"),
        ]
        eleitores = [
            Eleitor.objects.create(
                eleicao=eleicao,
                nome=f"Nome Particular {indice}",
                email=f"privado{indice}@example.com",
                matricula=f"privado-{indice}",
                ja_votou=True,
            )
            for indice in (1, 2)
        ]
        Eleitor.objects.create(
            eleicao=eleicao,
            nome="Eleitor Ausente",
            email="ausente@example.com",
            matricula="ausente",
        )
        Voto.objects.create(
            eleicao=eleicao,
            chapa=chapas[0],
            tipo_voto=Voto.Tipo.VALIDO,
            codigo_comprovante="ZZZ000000001",
        )
        Voto.objects.create(
            eleicao=eleicao,
            chapa=chapas[1],
            tipo_voto=Voto.Tipo.VALIDO,
            codigo_comprovante="AAA000000002",
        )
        eleicao.data_inicio = agora - timedelta(hours=2)
        eleicao.data_fim = agora - timedelta(hours=1)
        eleicao.save(update_fields=("data_inicio", "data_fim"))
        return eleicao

    def test_auditoria_publica_lista_somente_eleicoes_encerradas(self):
        eleicao_encerrada = self.criar_eleicao_encerrada_para_auditoria()
        url = reverse("eleicao:auditoria_publica")

        resposta = self.client.get(url)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, eleicao_encerrada.titulo)
        self.assertNotContains(resposta, self.eleicao.titulo)

    def test_auditoria_nao_exibe_resultados_de_eleicao_em_andamento_ou_agendada(self):
        Voto.objects.create(
            eleicao=self.eleicao,
            chapa=self.chapa,
            tipo_voto=Voto.Tipo.VALIDO,
            codigo_comprovante="OPEN00000001",
        )
        self.eleitor.ja_votou = True
        self.eleitor.save(update_fields=("ja_votou",))

        resposta_aberta = self.client.get(
            reverse("eleicao:auditoria_eleicao", args=[self.eleicao.pk])
        )
        eleicao_agendada = Eleicao.objects.create(
            titulo="Eleição agendada",
            descricao="",
            data_inicio=timezone.now() + timedelta(days=1),
            data_fim=timezone.now() + timedelta(days=2),
        )
        resposta_agendada = self.client.get(
            reverse("eleicao:auditoria_eleicao", args=[eleicao_agendada.pk])
        )

        for resposta in (resposta_aberta, resposta_agendada):
            self.assertEqual(resposta.status_code, 200)
            self.assertNotContains(resposta, "Hash SHA-256 do Boletim de Urna")
            self.assertNotContains(resposta, "Total de Votos Válidos")
            self.assertNotContains(resposta, "OPEN00000001")
        self.assertContains(resposta_aberta, "ainda está em andamento")
        self.assertContains(resposta_agendada, "ainda está agendada")

    def test_auditoria_verifica_hash_comprovantes_e_nao_expoe_dados_pessoais(self):
        eleicao = self.criar_eleicao_encerrada_para_auditoria()
        url = reverse("eleicao:auditoria_eleicao", args=[eleicao.pk])
        resposta = self.client.get(url)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Total de Eleitores Aptos")
        self.assertContains(resposta, "Total de Votos Válidos")
        self.assertContains(resposta, "Hash SHA-256 do Boletim de Urna")
        self.assertNotContains(resposta, "Nome Particular")
        self.assertNotContains(resposta, "privado1@example.com")
        self.assertNotContains(resposta, "privado-1")

        posicoes = [
            resposta.content.index(codigo.encode())
            for codigo in ("AAA000000002", "ZZZ000000001")
        ]
        self.assertEqual(posicoes, sorted(posicoes))

        hash_boletim = resposta.context["dados_boletim"]["assinatura_digital"]
        resposta_hash = self.client.get(url, {"verificar": hash_boletim})
        self.assertContains(resposta_hash, "Hash SHA-256 confirmado")

        resposta_comprovante = self.client.get(url, {"verificar": "aaa000000002"})
        self.assertContains(resposta_comprovante, "Comprovante encontrado")

        resposta_invalida = self.client.get(url, {"verificar": "NAO-EXISTE"})
        self.assertContains(resposta_invalida, "não encontrado neste Boletim")

    def test_tela_inicial_oculta_o_cabecalho_publico(self):
        resposta = self.client.get(reverse("eleicao:inicio"))

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "<header")
        self.assertNotContains(resposta, "Acesso administrativo")
        self.assertNotContains(resposta, "Comissão Eleitoral")
        self.assertContains(resposta, "Sua participação fortalece a comunidade.")

    def test_telas_de_votacao_nao_mostram_cabecalho_publico(self):
        url_convite = reverse("eleicao:votar", args=[self.eleitor.token_convite])
        resposta = self.client.get(url_convite)

        self.assertRedirects(resposta, reverse("eleicao:cabina"))
        self.assertEqual(
            self.client.session["token_votacao"], str(self.eleitor.token_convite)
        )
        resposta = self.client.get(reverse("eleicao:cabina"))
        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Votação DCE")
        self.assertNotContains(resposta, "Acesso administrativo")

        self.client.post(
            reverse("eleicao:cabina"),
            {"matricula": "001234"},
        )
        resposta = self.client.get(reverse("eleicao:urna"))

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Votação DCE")
        self.assertNotContains(resposta, "Acesso administrativo")

        resposta = self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{self.chapa.pk}"},
            follow=True,
        )

        self.assertContains(resposta, "Recibo de Votação")
        self.assertContains(resposta, "Comissão Eleitoral")
        self.assertContains(resposta, self.eleicao.titulo)
        self.assertContains(resposta, Voto.objects.get().codigo_comprovante)
        self.assertContains(resposta, "Data do registro")
        self.assertContains(resposta, "Boletim de Urna")
        self.assertContains(resposta, "Copiar código")
        self.assertContains(resposta, "Salvar Recibo em PDF")
        self.assertContains(resposta, "window.print()")
        self.assertContains(resposta, "Auditoria Pública")
        self.assertNotContains(resposta, "Votação DCE")
        self.assertNotContains(resposta, "Acesso administrativo")

    def test_link_de_convite_agendado_mostra_quando_a_votacao_abrira(self):
        agora = timezone.now()
        data_inicio = agora + timedelta(hours=2)
        eleicao_agendada = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="",
            data_inicio=data_inicio,
            data_fim=data_inicio + timedelta(hours=1),
        )
        eleitor_agendado = Eleitor.objects.create(
            eleicao=eleicao_agendada,
            nome="Eleitor convidado",
            email="convidado@example.com",
            matricula="009876",
        )
        url_convite = reverse("eleicao:votar", args=[eleitor_agendado.token_convite])

        resposta = self.client.get(url_convite)

        self.assertRedirects(resposta, reverse("eleicao:cabina"))
        resposta = self.client.get(reverse("eleicao:cabina"))
        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "eleicao/eleicao_agendada.html")
        self.assertContains(resposta, "Esta votação ainda não começou")
        self.assertContains(resposta, eleicao_agendada.titulo)
        self.assertContains(
            resposta,
            timezone.localtime(data_inicio).strftime("%d/%m/%Y %H:%M"),
        )
        self.assertContains(resposta, "este mesmo link")
        self.assertNotContains(resposta, 'name="matricula"')

        resposta_post = self.client.post(
            reverse("eleicao:cabina"), {"matricula": "009876"}
        )

        self.assertEqual(resposta_post.status_code, 200)
        self.assertTemplateUsed(resposta_post, "eleicao/eleicao_agendada.html")
        self.assertNotIn("eleitor_id", self.client.session)

        Eleicao.objects.filter(pk=eleicao_agendada.pk).update(
            data_inicio=timezone.now() - timedelta(minutes=1)
        )
        resposta_aberta = self.client.get(url_convite)

        self.assertRedirects(resposta_aberta, reverse("eleicao:cabina"))
        resposta_aberta = self.client.get(reverse("eleicao:cabina"))
        self.assertEqual(resposta_aberta.status_code, 200)
        self.assertTemplateUsed(resposta_aberta, "eleicao/validar_eleitor.html")

    def test_convite_valida_matricula_normalizada_e_registra_voto_sem_identidade(self):
        response = self.client.get(
            reverse("eleicao:votar", args=[self.eleitor.token_convite])
        )
        self.assertRedirects(response, reverse("eleicao:cabina"))

        response = self.client.post(
            reverse("eleicao:cabina"),
            {"matricula": "1234"},
        )
        self.assertRedirects(response, reverse("eleicao:urna"))

        response = self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{self.chapa.pk}"},
        )

        voto = Voto.objects.get()
        self.assertRedirects(
            response, reverse("eleicao:comprovante", args=[voto.codigo_comprovante])
        )
        self.assertTrue(self.eleitor.__class__.objects.get(pk=self.eleitor.pk).ja_votou)
        self.assertRegex(voto.codigo_comprovante, r"^[A-Z0-9]{12}$")
        campos_proibidos = {"eleitor", "ip", "user_agent"}
        self.assertFalse(any(field.name in campos_proibidos for field in Voto._meta.fields))
        self.assertIsNotNone(voto.data_registro)
        self.assertNotIn("token_votacao", self.client.session)

    def test_link_com_token_invalido_nao_chega_a_cabina(self):
        response = self.client.get("/votar/nao-e-uuid/")

        self.assertEqual(response.status_code, 404)

    def test_nao_contabiliza_duas_vezes_na_mesma_sessao(self):
        self.client.get(
            reverse("eleicao:votar", args=[self.eleitor.token_convite]),
        )
        self.client.post(
            reverse("eleicao:cabina"),
            {"matricula": "001234"},
        )
        self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{self.chapa.pk}"},
        )

        response = self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{self.chapa.pk}"},
        )

        self.assertRedirects(response, reverse("eleicao:inicio"))
        self.assertEqual(Voto.objects.count(), 1)

    def test_link_reaberto_apos_voto_pede_matricula_e_mostra_confirmacao(self):
        url_convite = reverse("eleicao:votar", args=[self.eleitor.token_convite])
        self.client.get(url_convite)
        self.client.post(reverse("eleicao:cabina"), {"matricula": "001234"})
        self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{self.chapa.pk}"},
        )
        self.assertEqual(Voto.objects.count(), 1)

        resposta = self.client.get(url_convite, follow=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme sua identidade")
        resposta = self.client.post(
            reverse("eleicao:cabina"), {"matricula": "001234"}
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "eleicao/voto_ja_registrado.html")
        self.assertContains(resposta, "Você já votou")
        self.assertNotContains(resposta, "Escolha seu voto")
        self.assertNotContains(resposta, 'name="escolha"')
        self.assertEqual(Voto.objects.count(), 1)

    def test_link_de_voto_registrado_mostra_resultado_apos_encerramento(self):
        agora = timezone.now()
        eleicao_encerrada = Eleicao.objects.create(
            titulo="Eleição anterior",
            descricao="Descrição",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        eleitor_encerrado = Eleitor.objects.create(
            eleicao=eleicao_encerrada,
            nome="Eleitor",
            email="encerrado@example.com",
            matricula="9000",
            ja_votou=True,
        )
        Voto.objects.create(eleicao=eleicao_encerrada, tipo_voto=Voto.Tipo.BRANCO)
        Eleicao.objects.filter(pk=eleicao_encerrada.pk).update(
            data_inicio=agora - timedelta(hours=2),
            data_fim=agora - timedelta(hours=1),
        )
        url_convite = reverse("eleicao:votar", args=[eleitor_encerrado.token_convite])

        resposta = self.client.get(url_convite, follow=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "eleicao/resultado_eleicao.html")
        self.assertContains(resposta, "Eleição encerrada")
        self.assertContains(resposta, "Resultado da eleição")
        self.assertContains(resposta, "Votos em branco")
        self.assertNotContains(resposta, "Escolha seu voto")
        self.assertNotContains(resposta, "Você já votou")
        self.assertNotContains(resposta, "Acesso administrativo")
        self.assertEqual(Voto.objects.filter(eleicao=eleicao_encerrada).count(), 1)

    def test_bloqueia_apos_cinco_matriculas_incorretas(self):
        url = reverse("eleicao:votar", args=[self.eleitor.token_convite])
        self.client.get(url)
        for _ in range(5):
            response = self.client.post(
                reverse("eleicao:cabina"), {"matricula": "errada"}
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(Voto.objects.count(), 0)

    def test_link_de_eleicao_encerrada_abre_resultado_sem_opcoes_de_voto(self):
        agora = timezone.now()
        eleicao_encerrada = Eleicao.objects.create(
            titulo="Eleição encerrada",
            descricao="Descrição",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        eleitor_encerrado = Eleitor.objects.create(
            eleicao=eleicao_encerrada,
            nome="Eleitor",
            email="encerrado@example.com",
            matricula="9000",
        )
        eleicao_encerrada.data_fim = agora - timedelta(minutes=1)
        eleicao_encerrada.save(update_fields=("data_fim",))

        response = self.client.get(
            reverse("eleicao:votar", args=[eleitor_encerrado.token_convite]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Eleição encerrada")
        self.assertContains(response, "Resultado da eleição")
        self.assertNotContains(response, "Escolha seu voto")

    def test_voto_branco_nao_faz_referencia_a_chapa(self):
        self.client.get(
            reverse("eleicao:votar", args=[self.eleitor.token_convite])
        )
        self.client.post(
            reverse("eleicao:cabina"),
            {"matricula": "001234"},
        )

        response = self.client.post(reverse("eleicao:urna"), {"escolha": Voto.Tipo.BRANCO})

        self.assertEqual(response.status_code, 302)
        voto = Voto.objects.get()
        self.assertEqual(voto.tipo_voto, Voto.Tipo.BRANCO)
        self.assertIsNone(voto.chapa)

    def test_status_da_eleicao_agendada_ou_encerrada(self):
        agora = timezone.now()
        eleicao_agendada = Eleicao.objects.create(
            titulo="Eleição futura",
            descricao="Descrição",
            data_inicio=agora + timedelta(minutes=1),
            data_fim=agora + timedelta(hours=1),
        )
        self.assertEqual(eleicao_agendada.status_atual, "AGENDADA")

        eleicao_agendada.ativa = False
        eleicao_agendada.save(update_fields=("ativa",))
        self.assertEqual(eleicao_agendada.status_atual, "ENCERRADA")

    def test_nao_permite_chapa_de_outra_eleicao(self):
        agora = timezone.now()
        outra_eleicao = Eleicao.objects.create(
            titulo="Outra eleição",
            descricao="Descrição",
            data_inicio=agora + timedelta(hours=1),
            data_fim=agora + timedelta(hours=2),
        )
        outra_chapa = Chapa.objects.create(eleicao=outra_eleicao, numero=1, nome="Outra")
        outra_eleicao.data_inicio = agora - timedelta(hours=1)
        outra_eleicao.save(update_fields=("data_inicio",))
        self.client.get(
            reverse("eleicao:votar", args=[self.eleitor.token_convite])
        )
        self.client.post(
            reverse("eleicao:cabina"),
            {"matricula": "001234"},
        )

        response = self.client.post(
            reverse("eleicao:urna"),
            {"escolha": f"chapa:{outra_chapa.pk}"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Eleitor.objects.get(pk=self.eleitor.pk).ja_votou)
        self.assertEqual(Voto.objects.count(), 0)
