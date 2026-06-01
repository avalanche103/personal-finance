from django.db.models import Q
from django.shortcuts import render

from apps.products.models import Product


def product_list(request):
    query = request.GET.get('q', '').strip()
    products = Product.objects.select_related('institution', 'currency')
    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(symbol__icontains=query)
            | Q(isin__icontains=query)
            | Q(product_type__icontains=query)
            | Q(institution__name__icontains=query)
        )

    context = {
        'products': products.order_by('name'),
        'query': query,
    }
    template_name = 'products/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'products/list.html'
    return render(request, template_name, context)
