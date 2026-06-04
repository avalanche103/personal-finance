from django.shortcuts import render


def settings_page(request):
    return render(request, 'core/settings.html')
