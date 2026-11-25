from django.contrib import admin
from django.core.exceptions import ValidationError

from .forms import ChapaForm
from .models import Chapa, Eleicao, Eleitor, Voto


def _sem_exclusao_em_lote(actions):
    actions.pop("delete_selected", None)
    return actions


@admin.register(Eleicao)
class EleicaoAdmin(admin.ModelAdmin):
    list_display = ("titulo", "data_inicio", "data_fim", "ativa")
    list_filter = ("ativa",)
    search_fields = ("titulo",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.status_atual == "AGENDADA"
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.status_atual == "AGENDADA"
        )

    def get_actions(self, request):
        return _sem_exclusao_em_lote(super().get_actions(request))

    def delete_queryset(self, request, queryset):
        if any(eleicao.status_atual != "AGENDADA" for eleicao in queryset):
            raise ValidationError("Eleições iniciadas ou encerradas não podem ser excluídas.")
        queryset.delete()


@admin.register(Chapa)
class ChapaAdmin(admin.ModelAdmin):
    form = ChapaForm
    list_display = ("numero", "nome", "eleicao")
    list_filter = ("eleicao",)
    search_fields = ("nome",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.eleicao.status_atual == "AGENDADA"
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.eleicao.status_atual == "AGENDADA"
        )

    def get_actions(self, request):
        return _sem_exclusao_em_lote(super().get_actions(request))

    def delete_queryset(self, request, queryset):
        if any(chapa.eleicao.status_atual != "AGENDADA" for chapa in queryset.select_related("eleicao")):
            raise ValidationError("Chapas de eleições iniciadas ou encerradas não podem ser excluídas.")
        queryset.delete()


@admin.register(Eleitor)
class EleitorAdmin(admin.ModelAdmin):
    list_display = ("nome", "email", "matricula", "eleicao", "ja_votou", "email_enviado")
    list_filter = ("eleicao", "ja_votou", "email_enviado")
    search_fields = ("nome", "email", "matricula")
    readonly_fields = ("token_convite",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None
            or obj.eleicao.status_atual == "AGENDADA"
            or not obj.ja_votou
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.eleicao.status_atual == "AGENDADA"
        )

    def get_readonly_fields(self, request, obj=None):
        campos = list(super().get_readonly_fields(request, obj))
        if obj and obj.eleicao.status_atual != "AGENDADA":
            campos.extend(
                ("nome", "matricula", "eleicao", "ja_votou", "email_enviado", "data_envio", "erro_envio")
            )
            if obj.ja_votou:
                campos.append("email")
        return tuple(dict.fromkeys(campos))

    def get_actions(self, request):
        return _sem_exclusao_em_lote(super().get_actions(request))

    def delete_queryset(self, request, queryset):
        if any(eleitor.eleicao.status_atual != "AGENDADA" for eleitor in queryset.select_related("eleicao")):
            raise ValidationError("Eleitores de eleições iniciadas ou encerradas não podem ser excluídos.")
        queryset.delete()


@admin.register(Voto)
class VotoAdmin(admin.ModelAdmin):
    list_display = ("codigo_comprovante", "eleicao", "tipo_voto", "chapa")
    list_filter = ("eleicao", "tipo_voto")
    readonly_fields = ("codigo_comprovante",)
