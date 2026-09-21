"""Build a synthetic interaction graph for module 3.

Every account this writes is prefixed `demo_`, so nothing it creates can be
mistaken for an observation. It exists because the module's honest answer to a
thin graph is to say nothing, and a console showing nothing teaches an analyst
nothing about what the module does when it does have something.

The graph is built to exercise all four outcomes at once:

- a ring of five accounts hitting the same four targets, which is reported as
  a campaign;
- three accounts linked pairwise with no common core, which is refused and
  written into the run's notes;
- a small group of accounts who interact with each other, which scores low;
- accounts with one or two interactions, which are not scored at all.

Run with --clear to remove it again.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from social.models import Campaign, InteractionEdge, SocialAccount, SocialPost
from social.tasks import detect_campaigns, refresh_graph_scores, score_post

PREFIX = 'demo_'

# A ring: five accounts, one target list between them.
RING = [f'{PREFIX}amplifier_{n}' for n in range(1, 6)]
RING_TARGETS = [f'{PREFIX}ministry', f'{PREFIX}regulator', f'{PREFIX}daily_post',
                f'{PREFIX}mayor']

# A chain: each overlaps the next enough to be linked, but no account is hit
# by all three. The detector has to refuse this one.
CHAIN = {
    f'{PREFIX}beat_writer_1': ['t1', 't2', 't3', 't4'],
    f'{PREFIX}beat_writer_2': ['t2', 't3', 't4', 't5'],
    f'{PREFIX}beat_writer_3': ['t3', 't4', 't5', 't6'],
}
CHAIN_TARGETS = [f'{PREFIX}topic_{n}' for n in range(1, 7)]

# A reciprocated group. Talks to people who talk back.
CIRCLE = [f'{PREFIX}reporter_a', f'{PREFIX}reporter_b', f'{PREFIX}reporter_c',
          f'{PREFIX}reporter_d']

# Accounts with almost no graph around them. These get no score at all, which
# is the state the console has to show without implying they are clean.
QUIET = [f'{PREFIX}quiet_1', f'{PREFIX}quiet_2']

POSTS = [
    # Scores negative: two lexicon terms, both negative.
    (RING[0], 'The report is misleading and the whole claim is false.'),
    # Scores as toxic.
    (RING[1], 'Only an idiot would believe this pathetic hoax.'),
    # Scores nothing. Not one word is in the lexicon, and the post is plainly
    # not neutral — this is the case the old scorer recorded as 0.00.
    (f'{PREFIX}beat_writer_1',
     'They knew what was in the water in March and signed it off anyway.'),
    # Scores positive.
    (CIRCLE[0], 'A clear and accurate account of a helpful meeting.'),
]


class Command(BaseCommand):
    help = 'Create (or remove) a synthetic social graph for module 3.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete the demo accounts and anything that hangs off them.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Allow the command to run with DEBUG off.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                'This writes made-up accounts into the database and DEBUG is '
                'off. Pass --force if that is really what you want.'
            )

        if options['clear']:
            return self.clear()
        return self.seed()

    def clear(self):
        accounts = SocialAccount.objects.filter(handle__startswith=PREFIX)
        removed = accounts.count()
        accounts.delete()
        # Campaigns are not cascaded by a many-to-many, so a cluster whose
        # every member has gone would otherwise sit in the list for ever.
        orphans = Campaign.objects.filter(members__isnull=True)
        orphaned = orphans.count()
        orphans.delete()
        self.stdout.write(
            f'Removed {removed} demo account(s) and {orphaned} emptied cluster(s).'
        )

    @transaction.atomic
    def seed(self):
        made = {}

        def account(handle):
            if handle not in made:
                made[handle], _ = SocialAccount.objects.get_or_create(
                    handle=handle,
                    platform=SocialAccount.Platform.X,
                    defaults={'display_name': handle.replace(PREFIX, '')},
                )
            return made[handle]

        def mention(source, target):
            InteractionEdge.objects.get_or_create(
                from_account=account(source),
                to_account=account(target),
                edge_type=InteractionEdge.EdgeType.MENTION,
            )

        for handle in RING:
            for target in RING_TARGETS:
                mention(handle, target)

        topics = dict(zip(['t1', 't2', 't3', 't4', 't5', 't6'], CHAIN_TARGETS))
        for handle, keys in CHAIN.items():
            for key in keys:
                mention(handle, topics[key])

        # Everyone in the circle mentions everyone else, so every interaction
        # is returned and the reciprocity signal reads low.
        for handle in CIRCLE:
            for other in CIRCLE:
                if handle != other:
                    mention(handle, other)

        for handle in QUIET:
            mention(handle, RING_TARGETS[0])

        for handle, text in POSTS:
            post, created = SocialPost.objects.get_or_create(
                account=account(handle), text=text
            )
            if created or post.scored_at is None:
                score_post.run(post.pk)

        scored = refresh_graph_scores.run()
        detect_campaigns.run()

        unscored = SocialAccount.objects.filter(bot_likelihood__isnull=True).count()
        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(made)} demo account(s). '
            f'{scored} account(s) read, {unscored} left unscored on purpose, '
            f'{Campaign.objects.count()} cluster(s) on record.'
        ))
