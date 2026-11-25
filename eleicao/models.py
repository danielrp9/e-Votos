import secrets
import string
import uuid

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class EleicaoQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if any(eleicao.status_atual != "AGENDADA" for eleicao in self):
            raise ValidationError("Eleições iniciadas ou encerradas não podem ser alteradas.")
        return super().update(**kwargs)

    def delete(self):
        if any(eleicao.status_atual != "AGENDADA" for eleicao in self):
            raise ValidationError("Eleições iniciadas ou encerradas não podem ser excluídas.")
        return super().delete()


class ChapaQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if any(chapa.eleicao.status_atual != "AGENDADA" for chapa in self.select_related("eleicao")):
            raise ValidationError("Chapas de eleições iniciadas ou encerradas não podem ser alteradas.")
        eleicao_id = kwargs.get("eleicao_id", kwargs.get("eleicao"))
        if eleicao_id is not None:
            eleicao = Eleicao.objects.get(pk=eleicao_id)
            if eleicao.status_atual != "AGENDADA":
                raise ValidationError("Não é possível vincular chapas a uma eleição iniciada.")
        return super().update(**kwargs)

    def delete(self):
        if any(chapa.eleicao.status_atual != "AGENDADA" for chapa in self.select_related("eleicao")):
            raise ValidationError("Chapas de eleições iniciadas ou encerradas não podem ser excluídas.")
        return super().delete()


class EleitorQuerySet(models.QuerySet):
    def update(self, **kwargs):
        campos_permitidos = {
            "email",
            "email_enviado",
            "data_envio",
            "erro_envio",
            "ja_votou",
        }
        for eleitor in self.select_related("eleicao"):
            if eleitor.eleicao.status_atual == "AGENDADA":
                continue
            if set(kwargs) - campos_permitidos or "matricula" in kwargs:
                raise ValidationError(
                    "Após o início da eleição, somente e-mail e dados de envio podem ser alterados."
                )
            if "email" in kwargs and eleitor.ja_votou:
                raise ValidationError("O e-mail não pode ser alterado após o eleitor votar.")
            if "ja_votou" in kwargs:
                raise ValidationError("O comparecimento não pode ser alterado em lote.")
        return super().update(**kwargs)

    def delete(self):
        if any(eleitor.eleicao.status_atual != "AGENDADA" for eleitor in self.select_related("eleicao")):
            raise ValidationError("Eleitores de eleições iniciadas ou encerradas não podem ser excluídos.")
        return super().delete()


class Eleicao(models.Model):
    objects = EleicaoQuerySet.as_manager()

    titulo = models.CharField(max_length=200)
    descricao = models.TextField()
    membros_comissao = models.TextField(blank=True)
    data_inicio = models.DateTimeField()
    data_fim = models.DateTimeField()
    ativa = models.BooleanField(default=True)

    @property
    def status_atual(self):
        agora = timezone.now()
        if not self.ativa or agora > self.data_fim:
            return "ENCERRADA"
        if agora < self.data_inicio:
            return "AGENDADA"
        return "ABERTA"

    def save(self, *args, **kwargs):
        existente = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if existente and existente.status_atual != "AGENDADA":
            raise ValidationError("Eleições iniciadas ou encerradas não podem ser alteradas.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        existente = type(self).objects.get(pk=self.pk)
        if existente.status_atual != "AGENDADA":
            raise ValidationError("Eleições iniciadas ou encerradas não podem ser excluídas.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.titulo


class Chapa(models.Model):
    objects = ChapaQuerySet.as_manager()

    eleicao = models.ForeignKey(Eleicao, on_delete=models.CASCADE, related_name="chapas")
    numero = models.IntegerField()
    nome = models.CharField(max_length=200)
    slogan = models.CharField(max_length=255, blank=True)
    foto = models.ImageField(upload_to="chapas/", blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("eleicao", "numero"), name="unique_numero_chapa_por_eleicao")
        ]
        ordering = ("numero",)

    def save(self, *args, **kwargs):
        existente = type(self).objects.filter(pk=self.pk).select_related("eleicao").first() if self.pk else None
        if existente and existente.eleicao.status_atual != "AGENDADA":
            raise ValidationError("Chapas de eleições iniciadas ou encerradas não podem ser alteradas.")
        eleicao = Eleicao.objects.get(pk=self.eleicao_id)
        if eleicao.status_atual != "AGENDADA":
            raise ValidationError("Não é possível cadastrar chapas em uma eleição iniciada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        chapa = type(self).objects.select_related("eleicao").get(pk=self.pk)
        if chapa.eleicao.status_atual != "AGENDADA":
            raise ValidationError("Chapas de eleições iniciadas ou encerradas não podem ser excluídas.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.numero} - {self.nome}"


class Eleitor(models.Model):
    objects = EleitorQuerySet.as_manager()

    eleicao = models.ForeignKey(Eleicao, on_delete=models.CASCADE, related_name="eleitores")
    nome = models.CharField(max_length=200)
    email = models.EmailField()
    matricula = models.CharField(max_length=50)
    token_convite = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email_enviado = models.BooleanField(default=False)
    data_envio = models.DateTimeField(null=True, blank=True)
    erro_envio = models.TextField(blank=True)
    ja_votou = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("eleicao", "email"), name="unique_email_eleitor_por_eleicao"),
            models.UniqueConstraint(fields=("eleicao", "matricula"), name="unique_matricula_eleitor_por_eleicao"),
        ]
        ordering = ("nome",)

    def save(self, *args, **kwargs):
        existente = (
            type(self).objects.select_related("eleicao").filter(pk=self.pk).first()
            if self.pk
            else None
        )
        if existente and existente.eleicao.status_atual != "AGENDADA":
            update_fields = kwargs.get("update_fields")
            campos_salvos = set(update_fields) if update_fields is not None else {
                "eleicao",
                "nome",
                "email",
                "matricula",
                "token_convite",
                "email_enviado",
                "data_envio",
                "erro_envio",
                "ja_votou",
            }
            if "matricula" in campos_salvos and self.matricula != existente.matricula:
                raise ValidationError("A matrícula não pode ser alterada após o início da eleição.")
            if "eleicao" in campos_salvos and self.eleicao_id != existente.eleicao_id:
                raise ValidationError("O eleitor não pode ser transferido após o início da eleição.")
            campos_permitidos = {
                "email",
                "email_enviado",
                "data_envio",
                "erro_envio",
                "ja_votou",
            }
            for campo in campos_salvos - campos_permitidos:
                if getattr(self, campo) != getattr(existente, campo):
                    raise ValidationError(
                        "Após o início da eleição, somente e-mail e dados de envio podem ser alterados."
                    )
            if (
                "email" in campos_salvos
                and self.email != existente.email
                and existente.ja_votou
            ):
                raise ValidationError("O e-mail não pode ser alterado após o eleitor votar.")
            if (
                "ja_votou" in campos_salvos
                and existente.ja_votou
                and not self.ja_votou
            ):
                raise ValidationError("O comparecimento registrado não pode ser removido.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        eleitor = type(self).objects.select_related("eleicao").get(pk=self.pk)
        if eleitor.eleicao.status_atual != "AGENDADA":
            raise ValidationError("Eleitores de eleições iniciadas ou encerradas não podem ser excluídos.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.nome


class Voto(models.Model):
    class Tipo(models.TextChoices):
        VALIDO = "VALIDO", "Válido"
        BRANCO = "BRANCO", "Branco"
        NULO = "NULO", "Nulo"

    eleicao = models.ForeignKey(Eleicao, on_delete=models.CASCADE, related_name="votos")
    chapa = models.ForeignKey(
        Chapa, on_delete=models.PROTECT, related_name="votos", null=True, blank=True
    )
    tipo_voto = models.CharField(max_length=10, choices=Tipo.choices)
    codigo_comprovante = models.CharField(max_length=12, unique=True, editable=False)
    data_registro = models.DateTimeField(default=timezone.now, editable=False)

    def save(self, *args, **kwargs):
        if not self.codigo_comprovante:
            caracteres = string.ascii_uppercase + string.digits
            self.codigo_comprovante = "".join(secrets.choice(caracteres) for _ in range(12))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.codigo_comprovante
