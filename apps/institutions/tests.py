from django.test import SimpleTestCase

from apps.institutions.branding import (
    institution_accent_color,
    institution_initials,
    institution_logo_path,
)


class InstitutionBrandingTests(SimpleTestCase):
    def test_logo_path_for_known_slug(self):
        self.assertEqual(
            institution_logo_path('finstore'),
            'img/institutions/finstore.svg',
        )
        self.assertEqual(
            institution_logo_path('nbrb'),
            'img/institutions/nbrb.svg',
        )

    def test_logo_path_unknown_slug(self):
        self.assertIsNone(institution_logo_path('unknown-bank'))

    def test_initials_from_name(self):
        self.assertEqual(institution_initials('БНБ-Банк'), 'ББ')
        self.assertEqual(institution_initials('Binance'), 'BI')

    def test_accent_color_fallback(self):
        self.assertEqual(institution_accent_color('finstore'), '#00A3E0')
        self.assertEqual(institution_accent_color('missing'), '#6B7280')
