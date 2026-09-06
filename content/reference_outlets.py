"""A standing panel of outlets to cross-reference against, registered or not.

Why this exists
---------------
Corroboration used to be able to see two things: articles this system had
already ingested, and the feeds of outlets someone had explicitly added as a
Source. On a fresh install that is one or two outlets, so almost every story
came back "no other outlet carried this" — which the scorer honestly reported
and which was nonetheless useless, because nobody had looked anywhere.

So this list is consulted regardless of whether a domain is a tracked Source.
None of these are registered here by default and none of them need to be. An
outlet we do not track is assumed to have neutral credibility, so including it
cannot inflate a score by much on its own; what it does is let the system
notice that four unrelated newsrooms carried the same story.

How the list was chosen
-----------------------
Wire services and large newsrooms across several countries and editorial
positions, weighted towards regions this deployment actually reads. It is not
a whitelist of truth and nothing here is treated as authoritative: these
outlets are *where we look*, and every one of them is scored by Module 1 on
the same terms as any other domain the moment it becomes a tracked Source.

They are read by RSS, which is the polite way to ask a newsroom what it just
published: one request per outlet, cached, rate-limited, and honouring
robots.txt like everything else in Module 2.

A feed here covers roughly the last day or two. For anything older, GDELT's
index is the backend that reaches back, and the two are used together.
"""

from __future__ import annotations

# (domain, feed url, short label). The domain is what a match is attributed
# to, and is the registrable domain so it lines up with core.Source.
REFERENCE_FEEDS: tuple[tuple[str, str, str], ...] = (
    # --- International ---
    ('bbc.co.uk', 'https://feeds.bbci.co.uk/news/world/rss.xml', 'BBC News'),
    ('theguardian.com', 'https://www.theguardian.com/world/rss', 'The Guardian'),
    ('aljazeera.com', 'https://www.aljazeera.com/xml/rss/all.xml', 'Al Jazeera'),
    ('npr.org', 'https://feeds.npr.org/1001/rss.xml', 'NPR'),
    ('cbc.ca', 'https://www.cbc.ca/webfeed/rss/rss-topstories', 'CBC'),
    ('dw.com', 'https://rss.dw.com/rdf/rss-en-world', 'Deutsche Welle'),
    ('france24.com', 'https://www.france24.com/en/rss', 'France 24'),
    ('abc.net.au', 'https://www.abc.net.au/news/feed/51120/rss.xml', 'ABC (Australia)'),
    ('skynews.com', 'https://feeds.skynews.com/feeds/rss/world.xml', 'Sky News'),
    ('cnbc.com', 'https://www.cnbc.com/id/100003114/device/rss/rss.html', 'CNBC'),

    # --- India, because this deployment reads Indian coverage ---
    ('thehindu.com', 'https://www.thehindu.com/news/national/feeder/default.rss', 'The Hindu'),
    ('indiatimes.com', 'https://timesofindia.indiatimes.com/rssfeedstopstories.cms', 'Times of India'),
    ('ndtv.com', 'https://feeds.feedburner.com/ndtvnews-top-stories', 'NDTV'),
    ('indianexpress.com', 'https://indianexpress.com/feed/', 'The Indian Express'),
    ('business-standard.com', 'https://www.business-standard.com/rss/home_page_top_stories.rss',
     'Business Standard'),
    ('thewire.in', 'https://thewire.in/rss', 'The Wire'),
    ('scroll.in', 'https://feeds.feedburner.com/ScrollinArticles.rss', 'Scroll.in'),
)

BY_DOMAIN = {domain: (url, label) for domain, url, label in REFERENCE_FEEDS}


def feeds_excluding(*domains: str) -> list[tuple[str, str, str]]:
    """The panel minus the outlets given — normally the article's own site.

    An outlet quoting itself is not a second opinion.
    """
    skip = {d for d in domains if d}
    return [row for row in REFERENCE_FEEDS if row[0] not in skip]
