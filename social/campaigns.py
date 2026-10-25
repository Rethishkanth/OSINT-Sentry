"""Finding groups of accounts that move together.

The per-account coordination score in social.analysis answers "does anyone
else hit the same targets as this account?". It cannot answer "who are the
*five* of them?", because the closest-peer number is the same 0.9 whether the
peer is one other account or forty.

This module answers the second question. It is deliberately not clustering on
anything learned: a campaign here is a set of accounts that all interact with
the same set of other accounts, which is a shape in the graph and nothing more.
A trained model subclasses BaseCampaignDetector and is named in
SOCIAL_CAMPAIGN_DETECTOR.

Two rules keep the output from flattering itself:

*A cluster must have a common core.* Linking accounts pairwise and taking the
connected components lets A join C through B without A and C having anything
in common, and a long enough chain will swallow the whole graph into one
"campaign". So a component is only reported if every member interacts with the
same `min_shared` accounts.

*What is rejected is recorded.* A pass that finds nothing writes down what it
looked at and why it stopped, in the run's notes. Otherwise an empty campaign
list reads as "there are no campaigns" when it means "three accounts overlapped
and the fourth never showed up".
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

from .analysis import AccountNode, plural, reason


@dataclass
class DetectedCampaign:
    """One cluster, as the detector found it."""

    account_ids: list[int]
    common_target_ids: list[int]
    cohesion: float
    name: str
    reasons: list[dict] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Identity of the cluster: its membership, nothing else.

        Re-running the detector over an unchanged graph has to land on the
        same row rather than stacking a second copy of every campaign. Adding
        a member makes it a different cluster, which is correct - it is.
        """
        members = ','.join(str(i) for i in sorted(self.account_ids))
        return hashlib.sha256(members.encode()).hexdigest()


@dataclass
class DetectionResult:
    campaigns: list[DetectedCampaign]
    notes: list[dict]
    accounts_considered: int
    detector_version: str


class BaseCampaignDetector(ABC):
    """Contract for grouping accounts into coordinated clusters."""

    detector_version: str = 'base'

    @abstractmethod
    def detect(self, accounts, edges) -> DetectionResult:
        """Group `accounts` given `edges`.

        `accounts` is a sequence of AccountNode, `edges` a sequence of
        (from_account_id, to_account_id). Returning no campaigns is a valid
        answer; returning no campaigns *and no notes* is not.
        """


class OverlapCampaignDetector(BaseCampaignDetector):
    """Clusters accounts by the company they keep.

    Two accounts are linked when they interact with at least `min_shared` of
    the same accounts and their target sets overlap by at least `min_overlap`
    (Jaccard). A group of `min_size` or more linked accounts that *all* share
    `min_shared` targets is reported as a campaign.

    Every campaign carries the caveat that a shape is not an intent. A press
    pack covering one ministry, a fandom replying to one account, and a paid
    amplification network are the same picture from here.
    """

    detector_version = 'overlap-v1'

    def __init__(self, min_size=None, min_shared=None, min_overlap=None):
        self.min_size = (
            min_size if min_size is not None
            else getattr(settings, 'SOCIAL_CAMPAIGN_MIN_SIZE', 3)
        )
        self.min_shared = (
            min_shared if min_shared is not None
            else getattr(settings, 'SOCIAL_MIN_SHARED_TARGETS', 3)
        )
        self.min_overlap = (
            min_overlap if min_overlap is not None
            else getattr(settings, 'SOCIAL_CAMPAIGN_MIN_OVERLAP', 0.5)
        )

    def detect(self, accounts, edges) -> DetectionResult:
        nodes = [
            node if isinstance(node, AccountNode) else AccountNode(id=node)
            for node in accounts
        ]
        by_id = {node.id: node for node in nodes}

        targets: dict[int, set[int]] = defaultdict(set)
        for from_id, to_id in edges:
            targets[from_id].add(to_id)

        eligible = sorted(
            account_id for account_id in by_id
            if len(targets[account_id]) >= self.min_shared
        )
        notes: list[dict] = []

        if len(eligible) < self.min_size:
            notes.append(reason(
                f'{len(eligible)} {plural(len(eligible), "account")} had enough '
                'targets to compare',
                f'A cluster needs {self.min_size} accounts each interacting with '
                f'{self.min_shared} or more others. {len(nodes)} '
                f'{plural(len(nodes), "account is", "accounts are")} tracked and '
                f'{len(eligible)} clear that bar, so no grouping was attempted. '
                'This is the size of the capture, not a finding about the '
                'accounts in it.',
                'limits',
            ))
            return DetectionResult([], notes, len(eligible), self.detector_version)

        links, pairs = self._link(eligible, targets)
        campaigns = []
        chained = 0

        for component in self._components(eligible, links):
            if len(component) < self.min_size:
                continue
            common = set.intersection(*(targets[i] for i in component))
            if len(common) < self.min_shared:
                # Linked in a chain rather than around a core. Reported as a
                # note, never as a campaign: A and C can sit in one component
                # without having interacted with a single account in common.
                chained += 1
                continue
            campaigns.append(self._describe(component, common, targets, by_id))

        if chained:
            notes.append(reason(
                f'{chained} {plural(chained, "group")} rejected for having no '
                'common core',
                f'Accounts in {chained} rejected {plural(chained, "group")} are '
                'linked to each other pairwise but do not all interact with the '
                f'same {self.min_shared} accounts. That is a chain of resemblances, '
                'not a group acting together, and reporting it as a campaign '
                'would eventually merge the whole graph into one.',
                'limits',
            ))

        if not campaigns and not chained:
            notes.append(reason(
                f'{pairs} overlapping {plural(pairs, "pair")} across '
                f'{len(eligible)} comparable accounts',
                f'No group of {self.min_size} or more accounts shares '
                f'{self.min_shared} targets at an overlap of {self.min_overlap:.2f} '
                'or better. Accounts can still be coordinating outside this '
                'capture, through targets nobody here tracks.',
                'limits',
            ))

        campaigns.sort(key=lambda c: (-len(c.account_ids), -c.cohesion))
        return DetectionResult(
            campaigns, notes, len(eligible), self.detector_version
        )

    def _link(self, eligible, targets):
        """Adjacency between accounts whose target sets overlap enough."""
        links: dict[int, set[int]] = defaultdict(set)
        pairs = 0
        for index, left in enumerate(eligible):
            for right in eligible[index + 1:]:
                shared = targets[left] & targets[right]
                if len(shared) < self.min_shared:
                    continue
                if len(shared) / len(targets[left] | targets[right]) < self.min_overlap:
                    continue
                links[left].add(right)
                links[right].add(left)
                pairs += 1
        return links, pairs

    @staticmethod
    def _components(eligible, links):
        """Connected components, breadth-first. No recursion: a wide graph."""
        seen: set[int] = set()
        for start in eligible:
            if start in seen:
                continue
            component = {start}
            queue = [start]
            seen.add(start)
            while queue:
                current = queue.pop()
                for neighbour in links[current]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        component.add(neighbour)
                        queue.append(neighbour)
            yield sorted(component)

    def _describe(self, component, common, targets, by_id):
        """Turn a component into a campaign with its working attached."""
        overlaps = [
            len(targets[a] & targets[b]) / len(targets[a] | targets[b])
            for index, a in enumerate(component)
            for b in component[index + 1:]
        ]
        cohesion = round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0

        target_labels = [
            (by_id.get(t) or AccountNode(id=t)).label for t in sorted(common)
        ]
        shown = ', '.join(target_labels[:3])
        if len(target_labels) > 3:
            shown += f' and {len(target_labels) - 3} other'
            shown += '' if len(target_labels) - 3 == 1 else 's'

        members = ', '.join(
            (by_id.get(m) or AccountNode(id=m)).label for m in component
        )

        reasons = [
            reason(
                f'{len(component)} accounts interact with the same '
                f'{len(common)} {plural(len(common), "account")}',
                f'{members} all interact with {shown}. Every member of the '
                'group hits every one of those targets - this is a common '
                'core, not a chain of pairwise resemblances.',
                'campaign',
            ),
            reason(
                f'Cohesion {cohesion:.2f}',
                'Mean overlap between every pair of members, where 1.00 means '
                'their target lists are identical. A high number says they are '
                'tightly aligned; it does not say how many accounts they '
                'ignored in common, which is the same thing said from the '
                'other side.',
                'campaign',
            ),
            reason(
                'A shape is not an intent',
                'This is a pattern in who interacts with whom. A press pack on '
                'one beat, a fan community around one account, and a paid '
                'amplification network all produce it. The cluster is where an '
                'analyst starts looking, not what they conclude.',
                'limits',
            ),
        ]

        return DetectedCampaign(
            account_ids=list(component),
            common_target_ids=sorted(common),
            cohesion=cohesion,
            name=f'{len(component)} accounts targeting {shown}',
            reasons=reasons,
        )


def get_campaign_detector() -> BaseCampaignDetector:
    path = getattr(
        settings, 'SOCIAL_CAMPAIGN_DETECTOR',
        'social.campaigns.OverlapCampaignDetector',
    )
    return import_string(path)()
