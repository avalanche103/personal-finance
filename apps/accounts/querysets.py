from django.db.models import Q, QuerySet

from apps.accounts.models import Account


def visible_account_queryset() -> QuerySet[Account]:
	return Account.objects.select_related('institution', 'currency').filter(
		Q(institution__slug='binance', current_balance__gt=0) | ~Q(institution__slug='binance')
	)
