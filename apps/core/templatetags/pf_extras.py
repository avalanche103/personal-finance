from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    if not mapping:
        return ''
    if isinstance(mapping, dict):
        value = mapping.get(key, '')
        return '' if value is None else value
    return ''
