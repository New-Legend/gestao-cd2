from django import template
from django.utils.safestring import mark_safe

from painel.registry import FIELD_LABELS

register = template.Library()


@register.filter
def attr(obj, name):
    try:
        field = obj._meta.get_field(name)
        if getattr(field, "choices", None):
            display = getattr(obj, f"get_{name}_display", None)
            if callable(display):
                return display()
    except Exception:
        pass
    value = getattr(obj, name, "")
    if callable(value):
        value = value()
    return value


@register.filter
def display_value(value):
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if value is None:
        return ""
    return value


@register.filter
def filesize_mb(value):
    try:
        return f"{int(value) / (1024 * 1024):.1f} MB"
    except (TypeError, ValueError):
        return "0 MB"


@register.filter
def field_label(module, name):
    try:
        model_field = module.model._meta.get_field(name)
        return FIELD_LABELS.get(name, model_field.verbose_name)
    except Exception:
        return FIELD_LABELS.get(name, str(name).replace("_", " ").title())


@register.filter
def form_field(form, name):
    try:
        return form[name]
    except Exception:
        return ""


@register.filter
def widget_type(bound_field):
    try:
        return bound_field.field.widget.input_type
    except Exception:
        return ""


@register.filter
def disabled_field(bound_field):
    try:
        return mark_safe(bound_field.as_widget(attrs={"disabled": "disabled"}))
    except Exception:
        return ""


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key, "")
    except Exception:
        return ""
