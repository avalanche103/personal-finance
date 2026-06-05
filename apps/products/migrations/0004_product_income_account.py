import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

	dependencies = [
		('accounts', '0001_initial'),
		('products', '0003_remove_product_term_months'),
	]

	operations = [
		migrations.AddField(
			model_name='product',
			name='income_account',
			field=models.ForeignKey(
				blank=True,
				null=True,
				on_delete=django.db.models.deletion.SET_NULL,
				related_name='income_products',
				to='accounts.account',
			),
		),
	]
