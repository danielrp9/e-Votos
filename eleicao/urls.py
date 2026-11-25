from django.urls import path

from . import painel_views, views

app_name = "eleicao"

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("auditoria/", views.auditoria_publica, name="auditoria_publica"),
    path(
        "auditoria/<int:eleicao_id>/",
        views.auditoria_publica,
        name="auditoria_eleicao",
    ),
    path("votar/cabina/", views.cabina, name="cabina"),
    path("votar/<uuid:token>/", views.votar, name="votar"),
    path("urna/", views.urna, name="urna"),
    path("comprovante/<str:codigo>/", views.comprovante, name="comprovante"),
    path("painel/", painel_views.painel, name="painel"),
    path("painel/login/", painel_views.painel_login, name="painel_login"),
    path("painel/logout/", painel_views.painel_logout, name="painel_logout"),
    path("painel/eleicoes/", painel_views.painel_eleicoes, name="painel_eleicoes"),
    path(
        "painel/eleicoes/nova/",
        painel_views.painel_eleicao_form,
        name="painel_eleicao_nova",
    ),
    path(
        "painel/eleicoes/<int:eleicao_id>/",
        painel_views.painel_eleicao_detalhes,
        name="painel_eleicao_detalhes",
    ),
    path(
        "painel/eleicoes/<int:eleicao_id>/editar/",
        painel_views.painel_eleicao_form,
        name="painel_eleicao_editar",
    ),
    path("painel/chapas/", painel_views.painel_chapas, name="painel_chapas"),
    path(
        "painel/chapas/nova/",
        painel_views.painel_chapa_form,
        name="painel_chapa_nova",
    ),
    path(
        "painel/chapas/<int:chapa_id>/editar/",
        painel_views.painel_chapa_form,
        name="painel_chapa_editar",
    ),
    path("painel/importar/", painel_views.painel_importar, name="painel_importar"),
    path(
        "painel/importar/modelo.csv",
        painel_views.painel_modelo_csv,
        name="painel_modelo_csv",
    ),
    path("painel/eleitores/", painel_views.painel_eleitores, name="painel_eleitores"),
    path("painel/apuracao/", painel_views.painel_apuracao, name="painel_apuracao"),
    path("painel/zeresima/", painel_views.painel_zeresima, name="painel_zeresima"),
    path(
        "painel/apuracao/comparecimento.csv",
        painel_views.painel_comparecimento_csv,
        name="painel_comparecimento_csv",
    ),
]
