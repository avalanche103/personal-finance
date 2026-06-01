from django.test import Client, TestCase

from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand


class DashboardSmokeTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def setUp(self):
		self.client = Client()

	def test_dashboard_and_reports_render(self):
		for url in ['/', '/exchange-rates/', '/portfolio-report/']:
			response = self.client.get(url)
			self.assertEqual(response.status_code, 200, url)

	def test_dashboard_contains_bootstrap_cards(self):
		response = self.client.get('/')
		self.assertContains(response, 'Latest NBRB rates')
		self.assertContains(response, 'USD')
		self.assertContains(response, 'Finstore')

	def test_portfolio_report_contains_bootstrap_institution(self):
		response = self.client.get('/portfolio-report/?as_of=2026-05-31')
		self.assertContains(response, 'Finstore')

# Create your tests here.
