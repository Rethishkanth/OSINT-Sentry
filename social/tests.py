"""Tests for module 3.

AbstentionTests is the important one. Both scorers here work by counting, and
counting always produces a number — the question is whether the number is
worth anything. An account that sent one mention nobody answered is 100%
unreciprocated; a post with no lexicon word in it balances to zero. Both come
out looking like findings. These tests check that neither is recorded as one.

The rest check the ordinary path: that a real signal still scores, and that
every score arrives with its working attached.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .analysis import (
    AccountNode,
    HeuristicGraphScorer,
    HeuristicTextScorer,
)
from .campaigns import OverlapCampaignDetector
from .models import (
    Campaign,
    CampaignRun,
    InteractionEdge,
    SocialAccount,
    SocialPost,
)
from .tasks import detect_campaigns, refresh_graph_scores, score_post

TEXT = HeuristicTextScorer()
DETECTOR = OverlapCampaignDetector()

# 1 links to 2 and 2 to 3, but 1 and 3 share no target at all. One connected
# component, no common core: the case the detector has to refuse.
CHAIN = (
    [(1, t) for t in (10, 11, 12, 13)]
    + [(2, t) for t in (11, 12, 13, 14)]
    + [(3, t) for t in (12, 13, 14, 15)]
)


def nodes(*handles):
    return [AccountNode(id=i, handle=h) for i, h in enumerate(handles, start=1)]


def effects(reasons):
    return {r['effect'] for r in reasons}


def by_id(scores):
    return {s.account_id: s for s in scores}


def account(handle, **kwargs):
    return SocialAccount.objects.create(
        handle=handle, platform=SocialAccount.Platform.X, **kwargs
    )


def edge(source, target):
    return InteractionEdge.objects.create(
        from_account=source,
        to_account=target,
        edge_type=InteractionEdge.EdgeType.MENTION,
    )


class AbstentionTests(TestCase):
    """What the scorers decline to judge, and whether they say so."""

    def test_a_single_unanswered_mention_is_not_a_bot_score(self):
        scored = by_id(HeuristicGraphScorer().score_graph(nodes('a', 'b'), [(1, 2)]))
        self.assertIsNone(scored[1].bot_likelihood)

    def test_declining_to_score_is_explained(self):
        scored = by_id(HeuristicGraphScorer().score_graph(nodes('a', 'b'), [(1, 2)]))
        self.assertIn('limits', effects(scored[1].reasons))

    def test_an_isolated_account_is_not_scored_zero(self):
        scored = by_id(HeuristicGraphScorer().score_graph(nodes('a'), []))
        self.assertIsNone(scored[1].bot_likelihood)
        self.assertIsNone(scored[1].coordination_score)

    def test_an_account_that_only_receives_is_not_judged_on_that(self):
        """Being mentioned by a thousand accounts says nothing about you."""
        edges = [(i, 1) for i in range(2, 12)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcdefghijk'), edges))
        self.assertIsNone(scored[1].bot_likelihood)
        detail = ' '.join(r['detail'] for r in scored[1].reasons)
        self.assertIn('what is said about it', detail)

    def test_two_accounts_sharing_one_target_are_not_coordinated(self):
        """A Jaccard of 1.00 over a single shared mention is arithmetic."""
        scored = by_id(
            HeuristicGraphScorer().score_graph(nodes('a', 'b', 'c'), [(1, 3), (2, 3)])
        )
        self.assertIsNone(scored[1].coordination_score)

    def test_a_post_with_no_lexicon_words_has_no_sentiment(self):
        result = TEXT.score_text('They knew what was in the water and signed it off.')
        self.assertIsNone(result.sentiment)
        self.assertIn('limits', effects(result.reasons))

    def test_an_empty_post_is_not_scored(self):
        result = TEXT.score_text('   ')
        self.assertIsNone(result.sentiment)
        self.assertIsNone(result.toxicity)

    def test_a_balanced_post_scores_zero_rather_than_abstaining(self):
        """Nothing-matched and matched-evenly are different states."""
        result = TEXT.score_text('An honest account and a misleading one.')
        self.assertEqual(result.sentiment, 0.0)


class GraphScoreTests(TestCase):
    """The ordinary path: a real signal still scores."""

    def test_an_unreciprocated_broadcaster_scores_high(self):
        edges = [(1, n) for n in range(2, 8)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcdefg'), edges))
        self.assertEqual(scored[1].bot_likelihood, 1.0)

    def test_a_fully_reciprocated_account_scores_zero(self):
        edges = [(1, 2), (2, 1), (1, 3), (3, 1), (1, 4), (4, 1)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcd'), edges))
        self.assertEqual(scored[1].bot_likelihood, 0.0)

    def test_partial_reciprocity_lands_between(self):
        edges = [(1, 2), (2, 1), (1, 3), (1, 4)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcd'), edges))
        self.assertAlmostEqual(scored[1].bot_likelihood, 2 / 3, places=3)

    def test_identical_target_lists_score_as_lockstep(self):
        edges = [(1, t) for t in (4, 5, 6)] + [(2, t) for t in (4, 5, 6)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcdef'), edges))
        self.assertEqual(scored[1].coordination_score, 1.0)

    def test_the_lockstep_reason_names_the_peer(self):
        edges = [(1, t) for t in (4, 5, 6)] + [(2, t) for t in (4, 5, 6)]
        scored = by_id(HeuristicGraphScorer().score_graph(nodes(*'abcdef'), edges))
        labels = ' '.join(r['label'] for r in scored[1].reasons)
        self.assertIn('@b', labels)

    def test_a_lone_actor_scores_zero_coordination(self):
        edges = [(1, t) for t in (2, 3, 4)] + [(5, t) for t in (6, 7, 8)]
        scored = by_id(
            HeuristicGraphScorer().score_graph(nodes(*'abcdefgh'), edges)
        )
        self.assertEqual(scored[1].coordination_score, 0.0)

    def test_every_account_gets_a_reason(self):
        edges = [(1, 2), (2, 1), (3, 1)]
        for score in HeuristicGraphScorer().score_graph(nodes(*'abc'), edges):
            self.assertTrue(score.reasons, f'account {score.account_id} has no working')

    def test_bare_ids_are_still_accepted(self):
        """The contract widened to AccountNode; a plain id must not crash."""
        scored = by_id(HeuristicGraphScorer().score_graph([1, 2], [(1, 2)]))
        self.assertIn(1, scored)

    def test_the_floors_are_configurable(self):
        scorer = HeuristicGraphScorer(min_interactions=1, min_shared_targets=1)
        scored = by_id(scorer.score_graph(nodes('a', 'b'), [(1, 2)]))
        self.assertEqual(scored[1].bot_likelihood, 1.0)


class TextScoreTests(TestCase):
    def test_positive_vocabulary_scores_positive(self):
        self.assertGreater(TEXT.score_text('An honest and accurate account.').sentiment, 0)

    def test_negative_vocabulary_scores_negative(self):
        self.assertLess(TEXT.score_text('A misleading and false claim.').sentiment, 0)

    def test_abuse_registers_as_toxicity(self):
        self.assertGreater(TEXT.score_text('You pathetic moron.').toxicity, 0)

    def test_toxicity_is_capped_at_one(self):
        self.assertLessEqual(TEXT.score_text('idiot moron scum').toxicity, 1.0)

    def test_a_civil_post_scores_zero_toxicity_with_a_caveat(self):
        result = TEXT.score_text('An honest account of the meeting.')
        self.assertEqual(result.toxicity, 0.0)
        detail = ' '.join(r['detail'] for r in result.reasons)
        self.assertIn('open name-calling only', detail)

    def test_the_matched_words_are_named(self):
        result = TEXT.score_text('This is a misleading hoax.')
        detail = ' '.join(r['detail'] for r in result.reasons)
        self.assertIn('misleading', detail)
        self.assertIn('hoax', detail)


class TaskTests(TestCase):
    def test_refresh_writes_scores_and_reasons(self):
        a, b, c, d = (account(h) for h in 'abcd')
        for target in (b, c, d):
            edge(a, target)
        refresh_graph_scores.run()

        a.refresh_from_db()
        self.assertEqual(a.bot_likelihood, 1.0)
        self.assertTrue(a.reasons)
        self.assertEqual(a.graph_model_version, 'heuristic-graph-v2')

    def test_refresh_leaves_a_thin_account_unscored(self):
        a, b = account('a'), account('b')
        edge(a, b)
        refresh_graph_scores.run()

        a.refresh_from_db()
        self.assertIsNone(a.bot_likelihood)
        self.assertIsNotNone(a.scored_at, 'it was looked at, it just was not judged')
        self.assertTrue(a.reasons)

    def test_an_empty_graph_scores_nothing(self):
        self.assertEqual(refresh_graph_scores.run(), 0)

    def test_scoring_a_post_records_its_working(self):
        post = SocialPost.objects.create(
            account=account('a'), text='A misleading and false claim.'
        )
        score_post.run(post.pk)

        post.refresh_from_db()
        self.assertLess(post.sentiment, 0)
        self.assertTrue(post.reasons)

    def test_scoring_a_deleted_post_is_not_an_error(self):
        self.assertIsNone(score_post.run(9999))


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))

    def test_the_account_list_carries_the_working(self):
        a, b, c, d = (account(h) for h in 'abcd')
        for target in (b, c, d):
            edge(a, target)
        refresh_graph_scores.run()

        rows = self.client.get('/api/social/accounts/').json()['results']
        scored = next(r for r in rows if r['handle'] == 'a')
        self.assertTrue(scored['reasons'])

    def test_the_post_list_carries_the_working(self):
        post = SocialPost.objects.create(account=account('a'), text='A false claim.')
        score_post.run(post.pk)

        rows = self.client.get('/api/social/posts/').json()['results']
        self.assertTrue(rows[0]['reasons'])

    def test_summary_counts_the_whole_table_not_one_page(self):
        for i in range(30):
            account(f'a{i}')
        body = self.client.get('/api/social/accounts/summary/').json()
        self.assertEqual(body['accounts'], 30)

    def test_summary_reports_the_unscored(self):
        a, b = account('a'), account('b')
        edge(a, b)
        refresh_graph_scores.run()

        body = self.client.get('/api/social/accounts/summary/').json()
        self.assertEqual(body['accounts_unscored'], 2)
        self.assertEqual(body['flagged'], 0)

    def test_summary_on_an_empty_database_is_not_an_error(self):
        body = self.client.get('/api/social/accounts/summary/').json()
        self.assertEqual(body['accounts'], 0)
        self.assertIsNone(body['mean_toxicity'])

    def test_flagged_excludes_the_unscored(self):
        a, b = account('a'), account('b')
        edge(a, b)
        refresh_graph_scores.run()

        body = self.client.get('/api/social/accounts/flagged/').json()
        self.assertEqual(body['count'], 0)

    def test_the_dashboard_shows_the_unscored_beside_the_flagged(self):
        """The overview panel renders three sub-metrics and drops the rest."""
        a, b = account('a'), account('b')
        edge(a, b)
        refresh_graph_scores.run()

        social = self.client.get('/api/overview/').json()['modules']['social']
        shown = [
            key for key in social
            if key not in ('online', 'accounts', 'model_version')
        ][:3]
        self.assertIn('unscored', shown)
        self.assertEqual(social['unscored'], 2)

    def test_a_post_can_be_rescored_inline(self):
        post = SocialPost.objects.create(account=account('a'), text='A false claim.')
        body = self.client.post(f'/api/social/posts/{post.pk}/score/').json()
        self.assertTrue(body['reasons'])

    def test_rescoring_the_graph_happens_before_the_response(self):
        """The console redraws on this response; a 202 would be a lie."""
        a, b, c, d = (account(h) for h in 'abcd')
        for target in (b, c, d):
            edge(a, target)

        body = self.client.post('/api/social/refresh-scores/').json()
        self.assertEqual(body['scored'], 4)

        a.refresh_from_db()
        self.assertEqual(a.bot_likelihood, 1.0)


class CampaignDetectionTests(TestCase):
    """What the detector groups, and — mostly — what it refuses to group."""

    def test_a_ring_hitting_the_same_targets_is_found(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        result = DETECTOR.detect(nodes(*'abcdefghi'), edges)
        self.assertEqual(len(result.campaigns), 1)
        self.assertEqual(result.campaigns[0].account_ids, [1, 2, 3])

    def test_the_shared_targets_are_named(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        found = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        self.assertEqual(found.common_target_ids, [7, 8, 9])

    def test_an_identical_ring_is_perfectly_cohesive(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        found = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        self.assertEqual(found.cohesion, 1.0)

    def test_two_accounts_are_not_a_campaign(self):
        edges = [(a, t) for a in (1, 2) for t in (7, 8, 9)]
        result = DETECTOR.detect(nodes(*'abcdefghi'), edges)
        self.assertEqual(result.campaigns, [])

    def test_a_chain_of_resemblances_is_not_a_campaign(self):
        """A links to B and B to C, but A and C share nothing."""
        result = DETECTOR.detect(nodes(*[str(i) for i in range(1, 18)]), CHAIN)
        self.assertEqual(result.campaigns, [])

    def test_a_rejected_chain_is_written_down(self):
        result = DETECTOR.detect(nodes(*[str(i) for i in range(1, 18)]), CHAIN)
        self.assertTrue(result.notes)
        self.assertIn('common core', ' '.join(n['label'] for n in result.notes))

    def test_unrelated_accounts_produce_a_note_not_silence(self):
        edges = [
            (a, t)
            for a, block in ((1, 10), (2, 20), (3, 30))
            for t in (block, block + 1, block + 2)
        ]
        result = DETECTOR.detect(nodes(*[str(i) for i in range(1, 33)]), edges)
        self.assertEqual(result.campaigns, [])
        self.assertTrue(result.notes, 'an empty result must say what it looked at')

    def test_a_thin_graph_says_so_rather_than_reporting_nothing(self):
        result = DETECTOR.detect(nodes('a', 'b'), [(1, 2)])
        self.assertEqual(result.campaigns, [])
        self.assertIn('enough', ' '.join(n['label'] for n in result.notes))

    def test_every_campaign_carries_the_intent_caveat(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        found = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        self.assertIn('limits', effects(found.reasons))
        self.assertIn(
            'shape is not an intent', ' '.join(r['label'] for r in found.reasons)
        )

    def test_membership_is_the_identity(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        first = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        again = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        self.assertEqual(first.fingerprint, again.fingerprint)

    def test_a_larger_ring_is_a_different_cluster(self):
        nine = nodes(*'abcdefghi')
        three = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        four = three + [(4, t) for t in (7, 8, 9)]
        self.assertNotEqual(
            DETECTOR.detect(nine, three).campaigns[0].fingerprint,
            DETECTOR.detect(nine, four).campaigns[0].fingerprint,
        )

    def test_the_name_says_who_is_targeted(self):
        edges = [(a, t) for a in (1, 2, 3) for t in (7, 8, 9)]
        found = DETECTOR.detect(nodes(*'abcdefghi'), edges).campaigns[0]
        self.assertIn('@g', found.name)


class CampaignTaskTests(TestCase):
    def build_ring(self):
        members = [account(f'ring{i}') for i in range(3)]
        targets = [account(f'target{i}') for i in range(3)]
        for member in members:
            for target in targets:
                edge(member, target)
        return members, targets

    def test_a_pass_stores_the_cluster_and_its_members(self):
        members, targets = self.build_ring()
        detect_campaigns.run()

        campaign = Campaign.objects.get()
        self.assertEqual(campaign.size, 3)
        self.assertCountEqual(
            campaign.members.values_list('pk', flat=True), [m.pk for m in members]
        )
        self.assertCountEqual(
            campaign.common_targets.values_list('pk', flat=True),
            [t.pk for t in targets],
        )

    def test_re_running_updates_rather_than_duplicates(self):
        self.build_ring()
        detect_campaigns.run()
        detect_campaigns.run()
        self.assertEqual(Campaign.objects.count(), 1)
        self.assertEqual(CampaignRun.objects.count(), 2)

    def test_a_pass_that_finds_nothing_is_still_logged(self):
        account('lonely')
        detect_campaigns.run()

        run = CampaignRun.objects.get()
        self.assertEqual(run.campaigns_found, 0)
        self.assertTrue(run.notes, 'an empty pass must say what it looked at')

    def test_a_pass_over_an_empty_graph_does_not_crash(self):
        self.assertIsNotNone(detect_campaigns.run())

    def test_a_cluster_the_newest_pass_missed_is_kept(self):
        members, _targets = self.build_ring()
        detect_campaigns.run()

        InteractionEdge.objects.filter(from_account=members[0]).delete()
        detect_campaigns.run()

        campaign = Campaign.objects.get()
        self.assertEqual(campaign.size, 3, 'the old cluster is history, not a mistake')
        self.assertLess(campaign.last_seen_at, CampaignRun.objects.first().created_at)

    def test_a_cluster_is_not_stale_the_moment_it_is_found(self):
        """last_seen_at is stamped from the run, not from a separate now()."""
        self.build_ring()
        detect_campaigns.run()

        run = CampaignRun.objects.first()
        self.assertEqual(
            Campaign.objects.filter(last_seen_at__lt=run.created_at).count(), 0
        )


class CampaignApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))

    def ring(self):
        members = [account(f'ring{i}') for i in range(3)]
        targets = [account(f'target{i}') for i in range(3)]
        for member in members:
            for target in targets:
                edge(member, target)

    def test_detect_runs_before_it_answers(self):
        self.ring()
        body = self.client.post('/api/social/campaigns/detect/').json()
        self.assertEqual(body['campaigns_found'], 1)
        self.assertEqual(Campaign.objects.count(), 1)

    def test_the_campaign_list_carries_members_and_working(self):
        self.ring()
        detect_campaigns.run()

        row = self.client.get('/api/social/campaigns/').json()['results'][0]
        self.assertEqual(len(row['members']), 3)
        self.assertTrue(row['reasons'])

    def test_latest_run_reports_no_pass_rather_than_an_all_clear(self):
        body = self.client.get('/api/social/campaigns/latest_run/').json()
        self.assertIsNone(body['run'])

    def test_latest_run_carries_the_notes_of_an_empty_pass(self):
        account('lonely')
        detect_campaigns.run()

        body = self.client.get('/api/social/campaigns/latest_run/').json()
        self.assertTrue(body['run']['notes'])

    def test_the_run_log_is_readable(self):
        detect_campaigns.run()
        body = self.client.get('/api/social/campaign-runs/').json()
        self.assertEqual(body['count'], 1)

    def test_campaigns_cannot_be_written_by_hand(self):
        response = self.client.post('/api/social/campaigns/', {'name': 'invented'})
        self.assertEqual(response.status_code, 405)
