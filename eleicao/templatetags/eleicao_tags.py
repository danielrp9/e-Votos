from decimal import Decimal

from django import template
from django.template.defaultfilters import floatformat


register = template.Library()


@register.filter
def percentual_legivel(valor, casas_minimas=2):
    percentual = Decimal(str(valor))
    casas_decimais = int(casas_minimas)
    if percentual:
        casas_decimais = max(casas_decimais, -percentual.adjusted())
    return floatformat(
        percentual,
        casas_decimais,
    )
