from django import template

from apps.institutions.branding import (
    institution_accent_color,
    institution_initials,
    institution_logo_path,
)

register = template.Library()


@register.inclusion_tag('institutions/partials/logo.html')
def institution_logo(institution, *, size='md'):
    slug = getattr(institution, 'slug', '') or ''
    name = getattr(institution, 'name', '') or ''
    return {
        'logo_path': institution_logo_path(slug),
        'initials': institution_initials(name),
        'accent_color': institution_accent_color(slug),
        'institution_name': name,
        'size': size,
    }
