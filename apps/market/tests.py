import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.institutions.models import FinancialInstitution
from apps.market.models import DepositBank, DepositOffer
from apps.market.services.bootstrap import ensure_portfolio_deposit_banks
from apps.market.services.deposit_offers.alfabank import parse_alfabank_listing
from apps.market.services.deposit_offers.base import (
	expand_atomic_offers,
	split_discrete_terms,
)
from apps.market.services.deposit_offers.belarusbank import parse_belarusbank_deposits
from apps.market.services.deposit_offers.bnb import parse_bnb_listing, parse_bnb_rate_table
from apps.market.services.deposit_offers.belapb import parse_belapb_product_page
from apps.market.services.deposit_offers.heuristic import parse_listing_cards
from apps.market.services.deposit_offers.mtbank import parse_mtbank_product_page
from apps.market.services.deposit_offers.neobank import parse_neobank_deposits
from apps.market.services.deposit_offers.sberbank import parse_sber_product_page
from apps.market.services.deposit_offers.technobank import parse_technobank_deposits
from apps.market.services.deposit_offers.sync import sync_deposit_offers
from apps.market.services.nbrb_banks import parse_nbrb_banks_html, sync_nbrb_banks

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


class DepositOfferParserTests(TestCase):
	def test_split_discrete_terms(self):
		self.assertEqual(split_discrete_terms('13 и 37 месяцев'), ['13 месяцев', '37 месяцев'])
		self.assertEqual(
			split_discrete_terms('60 или 100 дней, 5, 7, 13 месяцев'),
			['60 дней', '100 дней', '5 месяцев', '7 месяцев', '13 месяцев'],
		)
		# Continuous ranges stay as one token; expand_atomic concretizes them.
		self.assertEqual(
			split_discrete_terms('от 45 дней до 60 месяцев'),
			['от 45 дней до 60 месяцев'],
		)

	def test_expand_atomic_offers_splits_terms_and_dedupes(self):
		from apps.market.services.deposit_offers.base import ParsedDepositOffer

		offers = expand_atomic_offers(
			[
				ParsedDepositOffer(
					external_id='bb-1:BYN',
					name='Успешный',
					currency='BYN',
					rate_pct=None,
					rate_pct_max=Decimal('14.4'),
					term_text='13 и 37 месяцев',
					term_days_min=390,
					term_days_max=1110,
					min_amount=None,
					is_irrevocable=True,
					source_url='https://example.by/',
					raw={},
				),
				ParsedDepositOffer(
					external_id='bb-1:BYN-dup',
					name='Успешный',
					currency='BYN',
					rate_pct=None,
					rate_pct_max=Decimal('14.4'),
					term_text='13 месяцев',
					term_days_min=390,
					term_days_max=390,
					min_amount=None,
					is_irrevocable=True,
					source_url='https://example.by/',
					raw={},
				),
			]
		)
		self.assertEqual(len(offers), 2)
		self.assertEqual({o.term_text for o in offers}, {'13 месяцев', '37 месяцев'})
		self.assertTrue(all(o.term_days_min == o.term_days_max for o in offers))

	def test_expand_concretizes_ranges_without_ot_do(self):
		from apps.market.services.deposit_offers.base import ParsedDepositOffer

		offers = expand_atomic_offers(
			[
				ParsedDepositOffer(
					external_id='sber-1',
					name='Сохраняй',
					currency='BYN',
					rate_pct=Decimal('6.9'),
					rate_pct_max=Decimal('6.9'),
					term_text='от 35 до 60 дней',
					term_days_min=35,
					term_days_max=60,
					min_amount=None,
					is_irrevocable=True,
					source_url='https://example.by/',
					raw={},
				),
				ParsedDepositOffer(
					external_id='zepter-1',
					name='Вклады ОНЛАЙН',
					currency='BYN',
					rate_pct=None,
					rate_pct_max=Decimal('14.62'),
					term_text='35–1100 дней',
					term_days_min=35,
					term_days_max=1100,
					min_amount=None,
					is_irrevocable=None,
					source_url='https://example.by/',
					raw={},
				),
			]
		)
		self.assertEqual(len(offers), 1)
		self.assertEqual(offers[0].term_text, '60 дней')
		self.assertEqual(offers[0].term_days_min, 60)
		self.assertEqual(offers[0].term_days_max, 60)
		self.assertNotIn('от', offers[0].term_text.lower())
		self.assertNotIn('–', offers[0].term_text)

	def test_parse_belarusbank_json(self):
		payload = json.loads((FIXTURES / 'belarusbank_deposits_sample.json').read_text(encoding='utf-8'))
		offers = parse_belarusbank_deposits(payload)
		self.assertGreaterEqual(len(offers), 3)
		irrevocable = next(o for o in offers if 'безотзывный' in o.name.lower() and o.currency == 'BYN')
		self.assertEqual(irrevocable.rate_pct_max, Decimal('14.45'))
		self.assertIsNone(irrevocable.rate_pct)
		self.assertTrue(irrevocable.is_irrevocable)
		self.assertEqual(irrevocable.min_amount, Decimal('100'))
		usd = next(o for o in offers if o.currency == 'USD')
		self.assertEqual(usd.external_id, '6079:USD')

	def test_parse_alfabank_listing(self):
		html = (FIXTURES / 'alfabank_deposits_byn_sample.html').read_text(encoding='utf-8')
		offers = parse_alfabank_listing(html, page_url='https://www.alfabank.by/deposits/byn/')
		self.assertGreaterEqual(len(offers), 3)
		slivki = next(o for o in offers if 'сливки' in o.name.lower())
		self.assertEqual(slivki.currency, 'BYN')
		self.assertEqual(slivki.rate_pct_max, Decimal('14.6'))
		self.assertTrue(slivki.is_irrevocable)

	def test_parse_bnb_listing_and_table(self):
		listing = (FIXTURES / 'bnb_listing_sample.html').read_text(encoding='utf-8')
		products = parse_bnb_listing(listing)
		self.assertGreaterEqual(len(products), 2)
		table = (FIXTURES / 'bnb_irrevocable_table_sample.html').read_text(encoding='utf-8')
		offers = parse_bnb_rate_table(
			table,
			product_name='Безотзывный вклад',
			source_url='https://bnb.by/o-lichnom/sberezhenie/bezotzyvnyy-vklad/',
			is_irrevocable=True,
		)
		self.assertGreaterEqual(len(offers), 5)
		byn_13 = next(
			o for o in offers
			if o.currency == 'BYN' and '13' in o.term_text and o.rate_pct == Decimal('12.9')
		)
		self.assertTrue(byn_13.is_irrevocable)

	def test_parse_nbrb_banks_html(self):
		html = (FIXTURES / 'nbrb_banks_sample.html').read_text(encoding='utf-8')
		banks = parse_nbrb_banks_html(html)
		self.assertEqual(len(banks), 3)
		self.assertEqual(banks[0].reg_number, '56')
		self.assertEqual(banks[0].swift, 'AKBBBY2X')
		self.assertEqual(banks[0].external_key, 'nbrb-56')
		self.assertEqual(banks[2].entity_type, DepositBank.EntityType.NKFO)

	def test_parse_heuristic_listing_cards(self):
		html = (FIXTURES / 'heuristic_listing_sample.html').read_text(encoding='utf-8')
		offers = parse_listing_cards(html, listing_url='https://example.by/deposits/')
		self.assertGreaterEqual(len(offers), 1)
		names = ' '.join(o.name.lower() for o in offers)
		self.assertTrue('тестовый' in names or 'валютный' in names)
		self.assertTrue(all((o.rate_pct or o.rate_pct_max or 0) <= 20 for o in offers))

	def test_heuristic_rejects_insurance_rate_and_junk_title(self):
		from apps.market.services.deposit_offers.heuristic import parse_product_page

		html = '''
		<html><h1>Сравнение пакетов сервисов</h1>
		<p>Гарантия возмещения вкладов 100%</p>
		<p>Ставка 100%</p>
		</html>
		'''
		self.assertEqual(parse_product_page(html, source_url='https://bank.by/deposits/compare/'), [])

		html_ok = '''
		<html><h1>Безотзывный вклад «Сберегай» в бел. руб.</h1>
		<p>Ставка до 12.9% годовых</p>
		<p>Срок 13 месяцев</p>
		</html>
		'''
		offers = parse_product_page(html_ok, source_url='https://sber-bank.by/vklady/sberegaj')
		self.assertEqual(len(offers), 1)
		self.assertEqual(offers[0].currency, 'BYN')
		self.assertEqual(offers[0].rate_pct_max, Decimal('12.9'))
		self.assertNotIn('Срок от', offers[0].term_text)

	def test_parse_sberbank_product_page(self):
		html = (FIXTURES / 'sberbank_deposit_sample.html').read_text(encoding='utf-8')
		offers = parse_sber_product_page(
			html,
			source_url='https://www.sber-bank.by/deposit/conserve-unrecall/BYN/attributes',
		)
		self.assertEqual(len(offers), 1)
		offer = offers[0]
		self.assertEqual(offer.currency, 'BYN')
		self.assertEqual(offer.rate_pct, Decimal('12.9'))
		self.assertEqual(offer.min_amount, Decimal('500'))
		self.assertTrue(offer.is_irrevocable)
		self.assertIn('13', offer.term_text)

	def test_parse_mtbank_pricing_rows(self):
		html = (FIXTURES / 'mtbank_deposit_sample.html').read_text(encoding='utf-8')
		offers = parse_mtbank_product_page(
			html,
			source_url='https://www.mtbank.by/deposits/mtbelki-online/',
		)
		self.assertGreaterEqual(len(offers), 5)
		self.assertTrue(all(o.currency == 'BYN' for o in offers))
		self.assertTrue(all('белкам' not in o.name.lower() for o in offers))
		self.assertTrue(any(o.term_text == '14 месяцев' and o.rate_pct == Decimal('12.60') for o in offers))
		self.assertTrue(any(o.is_irrevocable is True for o in offers))
		self.assertTrue(any(o.is_irrevocable is False for o in offers))
		self.assertTrue(all('открыть онлайн' not in (o.term_text or '').lower() for o in offers))

	def test_parse_belapb_current_schedule(self):
		html = (FIXTURES / 'belapb_deposit_sample.html').read_text(encoding='utf-8')
		url = (
			'https://www.belapb.by/chastnomu-klientu/sberezheniya/vklady-i-scheta/'
			'vklad-depozit-plyus-k-stabilnosti-v-belorusskih-rublyah-onlayn/'
		)
		offers = parse_belapb_product_page(html, source_url=url)
		self.assertGreaterEqual(len(offers), 5)
		self.assertTrue(all(o.currency == 'BYN' for o in offers))
		self.assertTrue(any(o.term_text == '95 дней' and o.rate_pct == Decimal('6.40') for o in offers))
		self.assertTrue(any(o.term_text == '1500 дней' and o.rate_pct == Decimal('14.50') for o in offers))
		self.assertFalse(any((o.rate_pct or 0) >= Decimal('15') for o in offers))

		closed_url = (
			'https://www.belapb.by/chastnomu-klientu/sberezheniya/vklady-i-scheta/'
			'bankovskie-vklady-depozity-po-kotorym-zaklyuchenie-novykh-dogovorov-ne-osushchestvlyaetsya/'
		)
		self.assertEqual(parse_belapb_product_page(html, source_url=closed_url), [])

	def test_parse_neobank_embedded(self):
		html = (FIXTURES / 'neobank_deposits_sample.html').read_text(encoding='utf-8')
		offers = parse_neobank_deposits(html)
		self.assertGreaterEqual(len(offers), 1)
		self.assertTrue(any(o.currency == 'BYN' for o in offers))

	def test_parse_technobank_encoded(self):
		html = (FIXTURES / 'technobank_deposits_sample.html').read_text(encoding='utf-8')
		offers = parse_technobank_deposits(html)
		self.assertGreaterEqual(len(offers), 5)
		self.assertTrue(any(o.currency == 'BYN' and (o.rate_pct or o.rate_pct_max) for o in offers))


class DepositOfferSyncTests(TestCase):
	def setUp(self):
		FinancialInstitution.objects.create(name='Беларусбанк', slug='belarusbank')
		FinancialInstitution.objects.create(name='Альфабанк', slug='alfabank')
		FinancialInstitution.objects.create(name='БНБ-Банк', slug='bnb-bank')

	def test_sync_nbrb_banks_from_fixture(self):
		html = (FIXTURES / 'nbrb_banks_sample.html').read_text(encoding='utf-8')
		result = sync_nbrb_banks(html=html)
		self.assertEqual(
			result['fetched'],
			3,
		)
		self.assertEqual(result['created'], 3)
		bb = DepositBank.objects.get(parser_code='belarusbank')
		self.assertEqual(bb.reg_number, '56')
		self.assertEqual(bb.institution.slug, 'belarusbank')
		self.assertTrue(bb.website)

	def test_sync_offers_upsert_and_deactivate(self):
		ensure_portfolio_deposit_banks()
		bank = DepositBank.objects.get(parser_code='belarusbank')
		payload = json.loads((FIXTURES / 'belarusbank_deposits_sample.json').read_text(encoding='utf-8'))
		offers = parse_belarusbank_deposits(payload)

		with patch(
			'apps.market.services.deposit_offers.registry.ADAPTERS',
			{'belarusbank': lambda: offers},
		):
			# Re-import sync path uses get_adapter which reads ADAPTERS
			with patch(
				'apps.market.services.deposit_offers.sync.get_adapter',
				side_effect=lambda code: (lambda: offers) if code == 'belarusbank' else None,
			):
				result = sync_deposit_offers(parser_codes=['belarusbank'])

		self.assertEqual(result.ok, 1)
		self.assertGreaterEqual(DepositOffer.objects.filter(bank=bank, is_active=True).count(), 3)

		# Second sync with fewer offers deactivates missing ones.
		reduced = [offers[0]]
		with patch(
			'apps.market.services.deposit_offers.sync.get_adapter',
			side_effect=lambda code: (lambda: reduced) if code == 'belarusbank' else None,
		):
			sync_deposit_offers(parser_codes=['belarusbank'])

		active = DepositOffer.objects.filter(bank=bank, is_active=True)
		self.assertEqual(active.count(), 1)
		self.assertEqual(active.first().external_id, offers[0].external_id)
		self.assertTrue(DepositOffer.objects.filter(bank=bank, is_active=False).exists())


class DepositOffersViewTests(TestCase):
	def setUp(self):
		bank = DepositBank.objects.create(
			name='Test Bank',
			short_name='Test Bank',
			slug='test-bank',
			external_key='test-bank',
			parser_code='belarusbank',
		)
		DepositOffer.objects.create(
			bank=bank,
			name='Test BYN offer',
			currency='BYN',
			rate_pct=Decimal('12.5'),
			rate_pct_max=Decimal('12.5'),
			term_text='13 месяцев',
			term_days_min=390,
			term_days_max=390,
			external_id='test-byn',
			scraped_at=timezone.now(),
			is_active=True,
		)
		DepositOffer.objects.create(
			bank=bank,
			name='Test USD offer',
			currency='USD',
			rate_pct=Decimal('1.0'),
			rate_pct_max=Decimal('1.0'),
			term_text='12 месяцев',
			term_days_min=360,
			term_days_max=360,
			external_id='test-usd',
			scraped_at=timezone.now(),
			is_active=True,
		)

	def test_list_page_ok(self):
		response = self.client.get(reverse('market:deposit_offers'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Test BYN offer')

	def test_filter_by_currency(self):
		response = self.client.get(reverse('market:deposit_offers'), {'currency': 'USD'})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Test USD offer')
		self.assertNotContains(response, 'Test BYN offer')

	def test_filter_by_term_bucket(self):
		response = self.client.get(reverse('market:deposit_offers'), {'term': '366-730'})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Test BYN offer')
		self.assertNotContains(response, 'Test USD offer')

		response = self.client.get(reverse('market:deposit_offers'), {'term': '181-365'})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Test USD offer')
		self.assertNotContains(response, 'Test BYN offer')

		response = self.client.get(reverse('market:deposit_offers'), {'term': 'le90'})
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, 'Test BYN offer')
		self.assertNotContains(response, 'Test USD offer')
