"""Mint a console JWT for local development.

The analyst console keeps its token in the browser's local storage, which an
incognito window, a cleared profile or a fresh preview pane does not have. The
alternative to this command is typing a password into the login form, and a
password typed to get past your own dev server is a password that ends up in
a shell history or a screen recording.

Refuses to run with DEBUG off. A command that prints a working credential for
any account is a development convenience and nothing else.

    manage.py devtoken
    manage.py devtoken --user analyst --json
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Print a JWT for the local console. Development only.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user', default=None,
            help='Username. Defaults to the first superuser, then any user.',
        )
        parser.add_argument(
            '--json', action='store_true',
            help='Print only the token pair as JSON.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                'devtoken refuses to run with DEBUG off. It prints a working '
                'credential, which is a development convenience and not '
                'something a deployed system should offer.'
            )

        try:
            from rest_framework_simplejwt.tokens import RefreshToken
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise CommandError('djangorestframework-simplejwt is not installed') from exc

        model = get_user_model()
        if options['user']:
            user = model.objects.filter(username=options['user']).first()
            if user is None:
                raise CommandError(f'no user named {options["user"]!r}')
        else:
            user = (model.objects.filter(is_superuser=True).order_by('pk').first()
                    or model.objects.order_by('pk').first())
            if user is None:
                raise CommandError(
                    'there are no users. Create one with: manage.py createsuperuser'
                )

        refresh = RefreshToken.for_user(user)
        pair = {'access': str(refresh.access_token), 'refresh': str(refresh)}

        if options['json']:
            self.stdout.write(json.dumps(pair))
            return

        self.stdout.write(f'user: {user.get_username()}')
        self.stdout.write('')
        self.stdout.write('Paste into the console\'s devtools, then reload:')
        self.stdout.write('')
        self.stdout.write(
            f'localStorage.setItem("osint_access", "{pair["access"]}");\n'
            f'localStorage.setItem("osint_refresh", "{pair["refresh"]}");'
        )
