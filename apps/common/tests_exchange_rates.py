from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.common.services.exchange_rates import ensure_nbrb_rates_current


class EnsureNbrbRatesCurrentTests(TestCase):
	@patch('apps.common.services.exchange_rates.sync_nbrb_rate_history')
	@patch('apps.common.services.exchange_rates.latest_tracked_nbrb_rate_date')
	def test_skips_sync_when_rates_are_current(self, latest_date, sync_history):
		latest_date.return_value = date(2026, 6, 5)

		result = ensure_nbrb_rates_current(today=date(2026, 6, 5))

		self.assertIsNone(result)
		sync_history.assert_not_called()

	@patch('apps.common.services.exchange_rates.sync_nbrb_rate_history')
	@patch('apps.common.services.exchange_rates.latest_tracked_nbrb_rate_date')
	def test_syncs_from_latest_date_when_history_is_stale(self, latest_date, sync_history):
		latest_date.return_value = date(2026, 6, 3)
		sync_history.return_value = {'records_created': 6}

		result = ensure_nbrb_rates_current(today=date(2026, 6, 5))

		sync_history.assert_called_once_with(start_date=date(2026, 6, 3), end_date=date(2026, 6, 5))
		self.assertEqual(result['records_created'], 6)

	@patch('apps.common.services.exchange_rates.sync_nbrb_rate_history')
	@patch('apps.common.services.exchange_rates.latest_tracked_nbrb_rate_date')
	def test_syncs_recent_window_when_history_missing(self, latest_date, sync_history):
		latest_date.return_value = None
		today = timezone.localdate()

		ensure_nbrb_rates_current(today=today)

		sync_history.assert_called_once()
		self.assertEqual(sync_history.call_args.kwargs['end_date'], today)
