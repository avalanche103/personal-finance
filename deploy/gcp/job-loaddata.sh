#!/bin/sh
set -e

python manage.py flush --noinput
python manage.py loaddata data/cloud_fixture.json
