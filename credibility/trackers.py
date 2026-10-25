"""Known ad networks, trackers, and miners, by the hostname they load from.

A plain list rather than a clever classifier, because an analyst should be
able to read exactly why a site was flagged and disagree with it. Matching is
on the registrable domain, so `securepubads.g.doubleclick.net` and
`stats.g.doubleclick.net` both resolve to the doubleclick.net entry.

This is not exhaustive and is not meant to be - it covers the networks common
enough that their absence says something. A site with none of these is not
guaranteed clean; a site with ten of them is definitely monetised.
"""

from __future__ import annotations

# Serving advertising. Their presence is normal for commercial publishing and
# is reported, not condemned - the penalty is small and grows with volume.
AD_NETWORKS = {
    'doubleclick.net': 'Google Ad Manager',
    'googlesyndication.com': 'Google AdSense',
    'googleadservices.com': 'Google Ads',
    'amazon-adsystem.com': 'Amazon Ads',
    'adnxs.com': 'Xandr',
    'rubiconproject.com': 'Magnite',
    'pubmatic.com': 'PubMatic',
    'openx.net': 'OpenX',
    'criteo.com': 'Criteo',
    'criteo.net': 'Criteo',
    'taboola.com': 'Taboola',
    'outbrain.com': 'Outbrain',
    'revcontent.com': 'RevContent',
    'mgid.com': 'MGID',
    'media.net': 'Media.net',
    'adform.net': 'Adform',
    'smartadserver.com': 'Smart AdServer',
    'sharethrough.com': 'Sharethrough',
    'triplelift.com': 'TripleLift',
    'indexexchange.com': 'Index Exchange',
    'casalemedia.com': 'Index Exchange',
    '33across.com': '33Across',
    'sovrn.com': 'Sovrn',
    'yieldmo.com': 'Yieldmo',
    'teads.tv': 'Teads',
    'adsafeprotected.com': 'IAS',
    'moatads.com': 'Moat',
    'zedo.com': 'ZEDO',
    'infolinks.com': 'Infolinks',
    'bidvertiser.com': 'BidVertiser',
}

# Aggressive networks: pop-unders, forced redirects, push-notification spam.
# These are a different claim from "this site has ads" and carry real weight.
INTRUSIVE_AD_NETWORKS = {
    'propellerads.com': 'PropellerAds',
    'popads.net': 'PopAds',
    'popcash.net': 'PopCash',
    'adcash.com': 'Adcash',
    'exoclick.com': 'ExoClick',
    'juicyads.com': 'JuicyAds',
    'trafficjunky.com': 'TrafficJunky',
    'adsterra.com': 'Adsterra',
    'hilltopads.net': 'HilltopAds',
    'monetag.com': 'Monetag',
    'clickadu.com': 'Clickadu',
    'adspyglass.com': 'AdSpyglass',
}

# Analytics and behavioural tracking.
TRACKERS = {
    'google-analytics.com': 'Google Analytics',
    'googletagmanager.com': 'Google Tag Manager',
    'facebook.net': 'Meta Pixel',
    'hotjar.com': 'Hotjar session recording',
    'mixpanel.com': 'Mixpanel',
    'segment.com': 'Segment',
    'segment.io': 'Segment',
    'amplitude.com': 'Amplitude',
    'fullstory.com': 'FullStory session recording',
    'mouseflow.com': 'Mouseflow session recording',
    'crazyegg.com': 'Crazy Egg',
    'clarity.ms': 'Microsoft Clarity session recording',
    'quantserve.com': 'Quantcast',
    'scorecardresearch.com': 'Comscore',
    'chartbeat.com': 'Chartbeat',
    'chartbeat.net': 'Chartbeat',
    'parsely.com': 'Parse.ly',
    'newrelic.com': 'New Relic',
    'nr-data.net': 'New Relic',
    'statcounter.com': 'StatCounter',
    'yandex.ru': 'Yandex Metrica',
    'clicky.com': 'Clicky',
    'kissmetrics.com': 'Kissmetrics',
    'heap.io': 'Heap',
    'optimizely.com': 'Optimizely',
    'vwo.com': 'VWO',
    'branch.io': 'Branch',
    'appsflyer.com': 'AppsFlyer',
    'adjust.com': 'Adjust',
    'bugsnag.com': 'Bugsnag',
    'sentry.io': 'Sentry',
}

# Browser push-notification nagware. Distinct from analytics: it exists to
# interrupt the reader and keep interrupting them after they leave.
NOTIFICATION_NAGWARE = {
    'onesignal.com': 'OneSignal push prompts',
    'pushengage.com': 'PushEngage push prompts',
    'izooto.com': 'iZooto push prompts',
    'pushnami.com': 'Pushnami push prompts',
    'webpushr.com': 'Webpushr push prompts',
}

# In-browser cryptocurrency mining. Not a monetisation choice one discloses.
MINERS = {
    'coinhive.com': 'Coinhive miner',
    'coin-hive.com': 'Coinhive miner',
    'cryptoloot.pro': 'Crypto-Loot miner',
    'crypto-loot.com': 'Crypto-Loot miner',
    'jsecoin.com': 'JSEcoin miner',
    'webminepool.com': 'WebMinePool miner',
    'minero.cc': 'Minero miner',
    'coinimp.com': 'CoinIMP miner',
}

# Inline snippets that give a tracker away even when the script host is
# proxied or self-hosted.
INLINE_SIGNATURES = (
    ("gtag(", 'Google Analytics (gtag)'),
    ("_gaq.push", 'Google Analytics (legacy)'),
    ("ga('create'", 'Google Analytics'),
    ("fbq(", 'Meta Pixel'),
    ("dataLayer.push", 'Google Tag Manager dataLayer'),
    ("_paq.push", 'Matomo'),
    ("mixpanel.init", 'Mixpanel'),
    ("hj(", 'Hotjar'),
    ("clarity(", 'Microsoft Clarity'),
)

CATEGORIES = (
    ('miners', MINERS),
    ('intrusive_ads', INTRUSIVE_AD_NETWORKS),
    ('ads', AD_NETWORKS),
    ('nagware', NOTIFICATION_NAGWARE),
    ('trackers', TRACKERS),
)


def classify(domain: str) -> tuple[str, str] | None:
    """(category, human name) for a registrable domain, or None if unknown.

    Ordered by severity so a domain appearing in two lists is reported as the
    more serious of the two.
    """
    if not domain:
        return None
    for category, table in CATEGORIES:
        name = table.get(domain)
        if name:
            return category, name
    return None
