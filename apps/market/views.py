from django.contrib import messages
from django.db.models import Max, Q
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.market.models import DepositBank, DepositOffer
from apps.market.services.bootstrap import ensure_portfolio_deposit_banks
from apps.market.services.deposit_offers.base import TERM_FILTER_BUCKETS
from apps.market.services.deposit_offers.sync import sync_deposit_offers
from apps.market.services.nbrb_banks import sync_nbrb_banks

TERM_FILTER_LABELS = {
	'le90': 'До 3 мес.',
	'91-180': '3–6 мес.',
	'181-365': '6–12 мес.',
	'366-730': '1–2 года',
	'gt730': 'Более 2 лет',
}


def deposit_offers_list(request):
	query = request.GET.get('q', '').strip()
	currency = request.GET.get('currency', '').strip().upper()
	bank_slug = request.GET.get('bank', '').strip()
	irrevocable = request.GET.get('irrevocable', '').strip()
	term = request.GET.get('term', '').strip()

	offers = (
		DepositOffer.objects.filter(is_active=True, bank__is_active=True)
		.select_related('bank')
	)
	if query:
		offers = offers.filter(
			Q(name__icontains=query)
			| Q(bank__name__icontains=query)
			| Q(bank__short_name__icontains=query)
		)
	if currency:
		offers = offers.filter(currency=currency)
	if bank_slug:
		offers = offers.filter(bank__slug=bank_slug)
	if irrevocable == '1':
		offers = offers.filter(is_irrevocable=True)
	elif irrevocable == '0':
		offers = offers.filter(is_irrevocable=False)
	if term in TERM_FILTER_BUCKETS:
		bucket_min, bucket_max = TERM_FILTER_BUCKETS[term]
		offers = offers.annotate(
			term_low=Coalesce('term_days_min', 'term_days_max'),
			term_high=Coalesce('term_days_max', 'term_days_min'),
		).filter(term_low__isnull=False, term_high__isnull=False)
		if bucket_min is not None:
			offers = offers.filter(term_high__gte=bucket_min)
		if bucket_max is not None:
			offers = offers.filter(term_low__lte=bucket_max)

	banks_with_parser = DepositBank.objects.filter(is_active=True).exclude(parser_code='')
	banks_without_parser = DepositBank.objects.filter(is_active=True, parser_code='').order_by('name')
	currencies = (
		DepositOffer.objects.filter(is_active=True)
		.values_list('currency', flat=True)
		.distinct()
		.order_by('currency')
	)
	last_synced = banks_with_parser.aggregate(max_sync=Max('last_synced_at'))['max_sync']

	context = {
		'offers': offers,
		'query': query,
		'currency': currency,
		'bank_slug': bank_slug,
		'irrevocable': irrevocable,
		'term': term,
		'term_filters': TERM_FILTER_LABELS,
		'banks': banks_with_parser.order_by('name'),
		'banks_without_parser': banks_without_parser,
		'banks_without_parser_count': banks_without_parser.count(),
		'currencies': currencies,
		'last_synced': last_synced,
		'offers_count': offers.count(),
	}
	template_name = (
		'market/partials/offers_table.html'
		if request.headers.get('HX-Request') == 'true'
		else 'market/deposit_offers.html'
	)
	return render(request, template_name, context)


@require_POST
def deposit_offers_refresh(request):
	ensure_portfolio_deposit_banks()
	try:
		banks_result = sync_nbrb_banks()
	except Exception as exc:
		messages.warning(request, f'NBRB bank list sync skipped/failed: {exc}')
		banks_result = None

	result = sync_deposit_offers()
	parts = []
	if banks_result is not None:
		parts.append(
			f"banks fetched={banks_result['fetched']} "
			f"(+{banks_result['created']}/~{banks_result['updated']})"
		)
	parts.append(f'offers ok={result.ok} failed={result.failed} total={result.total_offers}')
	for item in result.banks:
		if item.error:
			messages.error(request, f'{item.bank_name}: {item.error}')
	if result.failed:
		messages.warning(request, 'Deposit offers refresh finished with errors. ' + '; '.join(parts))
	else:
		messages.success(request, 'Deposit offers refreshed. ' + '; '.join(parts))
	return redirect('market:deposit_offers')
