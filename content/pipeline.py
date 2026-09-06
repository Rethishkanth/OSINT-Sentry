"""Content analysis: NER, topic detection, and summarisation.

The plan calls for spaCy/BERT NER, BERTopic, and RAG summarisation against a
self-hosted LLM. None of those are installed, so this module defines the
interface they plug into and ships a dependency-free heuristic implementation
so the surrounding pipeline is real and testable.

To swap in the real thing, subclass BaseAnalyzer and point the CONTENT_ANALYZER
setting at it. An LLM-backed summariser belongs here too: it should call the
model over HTTP from inside a Celery task, never load weights into the Django
process.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

# Words that start a sentence and would otherwise look like a proper noun,
# plus the function words that dominate any term-frequency count. Both uses
# matter downstream: content.corroborate builds its search query out of the
# entities and topics produced here, and a query of "about, like, what"
# finds either nothing or everything.
STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'if', 'then', 'this', 'that',
    'these', 'those', 'it', 'its', 'he', 'she', 'they', 'we', 'you', 'i',
    'in', 'on', 'at', 'by', 'for', 'to', 'of', 'with', 'from', 'as', 'is',
    'was', 'were', 'be', 'been', 'has', 'have', 'had', 'will', 'would',
    'can', 'could', 'should', 'after', 'before', 'while', 'when', 'where',
    'his', 'her', 'their', 'our', 'said', 'says', 'according',
    'about', 'like', 'what', 'who', 'whom', 'whose', 'why', 'how', 'which',
    'there', 'here', 'them', 'him', 'us', 'me', 'my', 'your', 'yours',
    'also', 'just', 'than', 'into', 'over', 'under', 'out', 'off', 'up',
    'down', 'again', 'once', 'very', 'much', 'many', 'more', 'most', 'some',
    'any', 'all', 'both', 'each', 'few', 'other', 'such', 'only', 'own',
    'same', 'so', 'too', 'not', 'no', 'nor', 'do', 'does', 'did', 'done',
    'being', 'am', 'are', 'may', 'might', 'must', 'shall', 'get', 'got',
    'go', 'went', 'come', 'came', 'make', 'made', 'take', 'took', 'know',
    'knew', 'think', 'thought', 'see', 'saw', 'want', 'well', 'still',
    'even', 'back', 'now', 'new', 'one', 'two', 'first', 'last', 'because',
    'through', 'during', 'between', 'against', 'without', 'within', 'per',
}

# Deliberately a literal space rather than \s: `\s+` spans newlines, which
# joined a headline to the next block and produced "entities" like
# "Online\n\nAdvertisement". Capitalised words on separate lines are two
# things, not one name.
PROPER_NOUN_RE = re.compile(r'\b([A-Z][a-z]{2,}(?:[ ]+[A-Z][a-z]{2,})*)\b')
SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
WORD_RE = re.compile(r"[a-z][a-z'-]{3,}")

ORG_SUFFIXES = ('Inc', 'Ltd', 'Corp', 'Group', 'Agency', 'Ministry', 'Bank',
                'University', 'Institute', 'Committee', 'Council')


@dataclass
class EntitySpan:
    text: str
    label: str
    mention_count: int = 1
    confidence: float | None = None


@dataclass
class TopicHit:
    label: str
    weight: float
    keywords: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    entities: list[EntitySpan]
    topics: list[TopicHit]
    summary: str
    sentiment: float | None
    analyzer_version: str


class BaseAnalyzer(ABC):
    """Contract for the content-analysis model stack."""

    analyzer_version: str = 'base'

    @abstractmethod
    def extract_entities(self, text: str) -> list[EntitySpan]:
        ...

    @abstractmethod
    def detect_topics(self, text: str) -> list[TopicHit]:
        ...

    @abstractmethod
    def summarize(self, text: str) -> str:
        ...

    def sentiment(self, text: str) -> float | None:
        return None

    def analyze(self, text: str) -> AnalysisResult:
        return AnalysisResult(
            entities=self.extract_entities(text),
            topics=self.detect_topics(text),
            summary=self.summarize(text),
            sentiment=self.sentiment(text),
            analyzer_version=self.analyzer_version,
        )


class HeuristicAnalyzer(BaseAnalyzer):
    """Dependency-free stand-in for spaCy + BERTopic + RAG summarisation.

    Proper-noun matching for entities, term frequency for topics, and lead
    sentences for the summary. Good enough to exercise the pipeline; not good
    enough to draw conclusions from.
    """

    analyzer_version = 'heuristic-v1'
    max_entities = 25
    max_topics = 5
    summary_sentences = 3

    def extract_entities(self, text: str) -> list[EntitySpan]:
        counts: Counter[str] = Counter()
        for match in PROPER_NOUN_RE.finditer(text or ''):
            phrase = match.group(1).strip()
            if phrase.lower() in STOPWORDS:
                continue
            counts[phrase] += 1

        spans = []
        for phrase, count in counts.most_common(self.max_entities):
            spans.append(
                EntitySpan(text=phrase, label=self._label_for(phrase), mention_count=count)
            )
        return spans

    @staticmethod
    def _label_for(phrase: str) -> str:
        if any(phrase.endswith(suffix) for suffix in ORG_SUFFIXES):
            return 'ORG'
        # Two capitalised words with no org marker reads as a person's name
        # more often than not. This is a guess, and labelled as OTHER when it
        # is not confident.
        return 'PERSON' if len(phrase.split()) == 2 else 'OTHER'

    def detect_topics(self, text: str) -> list[TopicHit]:
        words = WORD_RE.findall((text or '').lower())
        counts = Counter(w for w in words if w not in STOPWORDS)
        if not counts:
            return []

        total = sum(counts.values())
        top = counts.most_common(self.max_topics)
        return [
            TopicHit(label=word, weight=round(count / total, 4), keywords=[word])
            for word, count in top
        ]

    def summarize(self, text: str) -> str:
        sentences = [s.strip() for s in SENTENCE_RE.split(text or '') if s.strip()]
        if not sentences:
            return ''
        return ' '.join(sentences[: self.summary_sentences])


def get_analyzer() -> BaseAnalyzer:
    path = getattr(settings, 'CONTENT_ANALYZER', 'content.pipeline.HeuristicAnalyzer')
    return import_string(path)()
