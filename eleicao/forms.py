from django import forms
from PIL import Image, UnidentifiedImageError

from .models import Chapa, Eleicao


CAMPO_CLASSE = (
    "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 "
    "text-sm focus:border-indigo-600 focus:outline-none focus:ring-2 focus:ring-indigo-100"
)


class FormularioPainel(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            campo.widget.attrs.setdefault("class", CAMPO_CLASSE)


class EleicaoForm(FormularioPainel):
    class Meta:
        model = Eleicao
        fields = (
            "titulo",
            "descricao",
            "membros_comissao",
            "data_inicio",
            "data_fim",
            "ativa",
        )
        labels = {
            "titulo": "Título",
            "descricao": "Descrição",
            "membros_comissao": "Membros da comissão",
            "data_inicio": "Início",
            "data_fim": "Encerramento",
            "ativa": "Eleição ativa",
        }
        help_texts = {
            "data_inicio": (
                "Cadastre chapas e eleitores antes deste horário; após o início, "
                "novos cadastros ficam bloqueados."
            ),
        }
        widgets = {
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "membros_comissao": forms.Textarea(
                attrs={"rows": 4, "placeholder": "Um nome por linha"}
            ),
            "data_inicio": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "data_fim": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        formato = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S")
        self.fields["data_inicio"].input_formats = formato
        self.fields["data_fim"].input_formats = formato
        self.fields["membros_comissao"].help_text = (
            "Informe um membro por linha. Esses nomes serão impressos na zerésima "
            "com espaço para assinatura."
        )

    def clean_membros_comissao(self):
        linhas = self.cleaned_data["membros_comissao"].splitlines()
        return "\n".join(nome.strip() for nome in linhas if nome.strip())

    def clean(self):
        dados = super().clean()
        inicio = dados.get("data_inicio")
        fim = dados.get("data_fim")
        if inicio and fim and fim <= inicio:
            self.add_error("data_fim", "O encerramento deve ocorrer após o início.")
        return dados


class ChapaForm(FormularioPainel):
    class Meta:
        model = Chapa
        fields = ("eleicao", "numero", "nome", "slogan", "foto")
        labels = {
            "eleicao": "Eleição",
            "numero": "Número",
            "nome": "Nome da chapa",
            "slogan": "Slogan",
            "foto": "Foto",
        }

    def clean_foto(self):
        foto = self.cleaned_data.get("foto")
        arquivo_enviado = self.files.get(self.add_prefix("foto"))
        if not foto or not arquivo_enviado:
            return foto
        if arquivo_enviado.size > 2 * 1024 * 1024:
            raise forms.ValidationError("A imagem deve ter no máximo 2 MB.")

        formatos_por_extensao = {
            ".png": "PNG",
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".webp": "WEBP",
        }
        extensao = foto.name.rsplit(".", 1)[-1].lower()
        extensao = f".{extensao}" if "." in foto.name else ""
        formato_esperado = formatos_por_extensao.get(extensao)
        if not formato_esperado:
            raise forms.ValidationError("Envie uma imagem PNG, JPG ou WEBP.")

        try:
            with Image.open(arquivo_enviado) as imagem:
                formato_real = imagem.format
                imagem.verify()
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as erro:
            raise forms.ValidationError("O arquivo enviado não é uma imagem válida.") from erro
        finally:
            arquivo_enviado.seek(0)

        if formato_real != formato_esperado:
            raise forms.ValidationError("A extensão do arquivo não corresponde à imagem.")
        return foto


class BaseImportacaoEleitoresForm(forms.Form):
    eleicao = forms.ModelChoiceField(
        queryset=Eleicao.objects.all(), label="Eleição"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            campo.widget.attrs["class"] = CAMPO_CLASSE


class ImportacaoArquivoForm(BaseImportacaoEleitoresForm):
    arquivo = forms.FileField(
        label="Arquivo CSV ou Excel (.csv, .xlsx)",
        help_text="A primeira linha deve conter nome, email e matricula.",
    )

    def clean_arquivo(self):
        arquivo = self.cleaned_data["arquivo"]
        if not arquivo.name.lower().endswith((".csv", ".xlsx")):
            raise forms.ValidationError("Envie um arquivo .csv ou .xlsx.")
        return arquivo


class ImportacaoTextoForm(BaseImportacaoEleitoresForm):
    texto = forms.CharField(
        label="Linhas para colar",
        widget=forms.Textarea(attrs={"rows": 8}),
    )
