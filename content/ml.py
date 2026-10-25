"""Module 2's analyzer, built on two published transformer models.

The plan named spaCy/BERTopic for this module and a RAG summariser behind a
self-hosted LLM. What ships here is a BERT token classifier for the entities
and a sentence-embedding model for the topics and the summary. The difference
is worth stating plainly rather than burying:

* Entities are a real model doing the job the plan asked for. dslim/bert-base-NER
  is a BERT fine-tuned on CoNLL-2003, and it replaces a regular expression
  that called any two capitalised words a person. Measured on the CoNLL-2003
  test split by `manage.py evaluate_ner`; the numbers, and the confidence
  floor they justify, are in content/artifacts/bert_ner.metrics.json.
* Topics are keyphrases ranked by sentence-embedding similarity to the
  document, in the manner of KeyBERT. This is not BERTopic. BERTopic clusters
  a corpus and names the clusters, which needs a corpus resembling what an
  analyst will later paste in; fitting one on somebody else's news archive
  and calling its clusters the topic of today's article would be a worse
  answer than this one, not a better one.
* The summary is extractive: the sentences closest to the document's own
  embedding, in their original order. It is not RAG and does not claim to be.
  Nothing here contacts a language model.
* Sentiment stays None. There are good sentiment models, and Module 3 uses
  one, but they are tuned on short social text. Running one over a news
  article and recording the number as that article's sentiment is exactly the
  bad rating this platform declines to give.

Both models are downloaded on demand, cached under CONTENT_MODEL_CACHE and
never committed. `manage.py fetch_content_models` fetches them ahead of time.
Loading is deferred to first use and memoised per process, so importing this
module costs nothing and Django starts without the weights on disk.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from functools import lru_cache

from django.conf import settings

from .pipeline import (
    SENTENCE_RE,
    STOPWORDS,
    BaseAnalyzer,
    EntitySpan,
    TopicHit,
)

# CoNLL-2003's four tags, mapped onto the labels content.models.Entity stores.
# LOC becomes GPE because that is the name this schema gives a place, and MISC
# becomes OTHER because MISC is CoNLL's own name for "none of the above".
LABEL_MAP = {
    'PER': 'PERSON',
    'ORG': 'ORG',
    'LOC': 'GPE',
    'MISC': 'OTHER',
}

# A chunk this size stays well inside BERT's 512-wordpiece window even for
# text that tokenises badly, so nothing is silently truncated away. Sentences
# are kept whole: an entity split across two chunks is an entity lost twice.
CHUNK_CHARS = 1200

# Candidate keyphrases are 1 to 3 words. Longer phrases score well against the
# document for the wrong reason: they contain more of it.
PHRASE_WORDS = (1, 2, 3)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")

# Candidates are generated inside these segments and never across them. A
# phrase that steps over a full stop or a comma is not a phrase: sliding a
# window along the whole document produced "orders slow shares", which is the
# end of one sentence welded to the start of the next.
SEGMENT_RE = re.compile(r'[^A-Za-z\'\- ]+')

# The only words allowed in the middle of a three-word candidate. English
# builds a compound noun by joining two content words with one of these, so
# "ministry of health" and "food and water" survive while "warned that
# cholera" and "orders had slowed" do not: those are clauses, and a clause is
# a sentence fragment rather than a name for a topic.
PHRASE_CONNECTORS = {
    'of', 'the', 'for', 'and', 'in', 'on', 'at', 'to', 'de', 'del', 'la',
}

# Trimmed from both ends of an aggregated NER span.
ENTITY_EDGE_CHARS = '\'"`.,;:()[]{} \t\n'


def _cache_dir() -> str:
    return str(getattr(settings, 'CONTENT_MODEL_CACHE', '.model-cache'))


@lru_cache(maxsize=1)
def _ner_pipeline():
    """The token classifier, loaded once per process.

    HF_HOME is set here rather than in settings because it has to reach the
    environment before transformers is imported, and settings are read long
    before anyone asks for a model.
    """
    os.environ.setdefault('HF_HOME', _cache_dir())
    from transformers import pipeline

    return pipeline(
        'token-classification',
        model=getattr(settings, 'CONTENT_NER_MODEL', 'dslim/bert-base-NER'),
        # The aggregation is not a free choice here, because the confidence
        # floor downstream is only as meaningful as the score it filters on.
        #
        # 'simple' groups by tag without reassembling wordpieces, so a name
        # the tokeniser split arrives as fragments: 'Nadim Ladki' comes back
        # as Na, ##di, ##m La, ##ki, four entities that are not entities.
        # 'average' reassembles correctly but averages the continuation
        # pieces into the score, which marks a name down for being long
        # rather than for being doubtful: the same 'Nadim Ladki' scores
        # 0.7557 where 'first' gives it 0.9979. 'max' reports the most
        # confident wordpiece, which is the one number here guaranteed not to
        # understate.
        #
        # 'first' takes the leading wordpiece's probability, which is the
        # conventional reading and the one that leaves a low score meaning
        # what a low score should mean. On an all-capitals sports headline it
        # scores JAPAN at 0.48 and CHINA at 0.64, and the floor catches them.
        aggregation_strategy='first',
    )


@lru_cache(maxsize=1)
def _embedder():
    os.environ.setdefault('HF_HOME', _cache_dir())
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(
        getattr(settings, 'CONTENT_EMBEDDING_MODEL',
                'sentence-transformers/all-MiniLM-L6-v2'),
        cache_folder=_cache_dir(),
    )


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on sentence boundaries into pieces the model can read whole.

    A sentence longer than the limit on its own is passed through rather than
    cut: the tokeniser will truncate it, and losing the tail of one runaway
    sentence beats losing the boundary between two real ones.
    """
    sentences = [s for s in SENTENCE_RE.split(text or '') if s.strip()]
    chunks: list[str] = []
    current = ''
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f'{current} {sentence}'.strip() if current else sentence
    if current:
        chunks.append(current)
    return chunks


def clean_entity(word: str) -> str:
    """Trim the debris an aggregated NER span can carry.

    The tokeniser reassembles wordpieces, and the reassembly keeps stray
    quotes and punctuation that are not part of the name.
    """
    return (word or '').strip(ENTITY_EDGE_CHARS)


def span_text(span: dict, text: str) -> str:
    """The entity as the document spells it.

    The pipeline reports both a reassembled `word` and the character offsets
    it came from, and the two do not always agree: reassembling U.S. from
    wordpieces yields 'U. S', with a space the document does not contain and
    a full stop it does. Slicing the source avoids inventing a spelling, and
    falls back to the reassembly only when a span arrives without offsets.
    """
    start, end = span.get('start'), span.get('end')
    if start is None or end is None:
        return clean_entity(span.get('word', ''))
    return clean_entity(text[start:end])


def candidate_phrases(text: str) -> list[str]:
    """Word n-grams that could name a topic.

    A phrase that starts or ends on a stopword is dropped: "of the ministry"
    and "ministry of" are both worse names for the same thing than "ministry
    of health", and the embedding cannot tell the difference on its own. A
    three-word phrase additionally has to be joined by a connector rather
    than by a verb, which is the line between a topic and a clause.

    Returned in the order the document says them, so a caller that takes only
    the first N keeps the opening of the article rather than keeping every
    single word and no phrases.
    """
    seen: dict[str, None] = {}
    for segment in SEGMENT_RE.split(text or ''):
        lowered = [w.lower() for w in TOKEN_RE.findall(segment)]
        for i in range(len(lowered)):
            for size in PHRASE_WORDS:
                if i + size > len(lowered):
                    break
                window = lowered[i:i + size]
                if any(edge in STOPWORDS or len(edge) < 3
                       for edge in (window[0], window[-1])):
                    continue
                if size == 3 and window[1] not in PHRASE_CONNECTORS:
                    continue
                seen.setdefault(' '.join(window), None)
    return list(seen)


class TransformerAnalyzer(BaseAnalyzer):
    """Entities from a fine-tuned BERT, topics and summary from embeddings."""

    analyzer_version = 'bert-ner-v1+minilm-v1'
    is_trained = True

    max_entities = 25
    max_topics = 5
    summary_sentences = 3
    # How many candidate phrases to embed. Embedding every n-gram of a long
    # article is CPU spent on phrases that were never going to win; 1500 is
    # about a 10,000-word document, and the cut falls at the end of the text
    # rather than on a whole phrase length.
    max_candidates = 1500

    def __init__(self, min_confidence=None, min_similarity=None):
        self.min_confidence = (
            getattr(settings, 'CONTENT_NER_MIN_CONFIDENCE', 0.90)
            if min_confidence is None else min_confidence
        )
        self.min_similarity = (
            getattr(settings, 'CONTENT_TOPIC_MIN_SIMILARITY', 0.25)
            if min_similarity is None else min_similarity
        )

    # -- entities ---------------------------------------------------------

    def extract_entities(self, text: str) -> list[EntitySpan]:
        chunks = chunk_text(text)
        if not chunks:
            return []

        # Scores are averaged over mentions rather than maxed. A name the
        # model was sure of once and unsure of four times is not a name it
        # was sure of.
        totals: dict[tuple[str, str], float] = defaultdict(float)
        counts: dict[tuple[str, str], int] = defaultdict(int)

        for chunk, spans in zip(chunks, _ner_pipeline()(chunks)):
            for span in spans:
                word = span_text(span, chunk)
                label = LABEL_MAP.get(span.get('entity_group', ''), 'OTHER')
                # Only length is checked, not a stopword list. The obvious
                # filter here would drop any entity whose text is a common
                # word, and it would take May, Will, Mark and March with it.
                # Deciding that 'the' is not a person is the model's job, and
                # it is better at it than a word list.
                if len(word) < 2:
                    continue
                key = (word, label)
                totals[key] += float(span.get('score', 0.0))
                counts[key] += 1

        found = []
        for (word, label), count in counts.items():
            confidence = totals[(word, label)] / count
            if confidence < self.min_confidence:
                continue
            found.append(EntitySpan(
                text=word, label=label, mention_count=count,
                confidence=round(confidence, 4),
            ))

        found.sort(key=lambda s: (-s.mention_count, -(s.confidence or 0), s.text))
        return found[:self.max_entities]

    # -- topics -----------------------------------------------------------

    def detect_topics(self, text: str) -> list[TopicHit]:
        candidates = candidate_phrases(text)
        if not candidates:
            return []
        candidates = candidates[:self.max_candidates]

        model = _embedder()
        document = model.encode(text or '', normalize_embeddings=True)
        phrases = model.encode(candidates, normalize_embeddings=True,
                               batch_size=64, show_progress_bar=False)
        scores = phrases @ document

        ranked = sorted(zip(candidates, scores), key=lambda pair: -pair[1])
        chosen: list[tuple[str, float]] = []
        for phrase, score in ranked:
            if score < self.min_similarity:
                break
            # Two phrases sharing a word are usually the same topic said
            # twice; "health ministry" and "ministry" should not both take a
            # slot from a topic nobody has heard yet.
            words = set(phrase.split())
            if any(words & set(taken.split()) for taken, _ in chosen):
                continue
            chosen.append((phrase, float(score)))
            if len(chosen) == self.max_topics:
                break

        return [
            TopicHit(label=phrase, weight=round(score, 4), keywords=phrase.split())
            for phrase, score in chosen
        ]

    # -- summary ----------------------------------------------------------

    def summarize(self, text: str) -> str:
        sentences = [s.strip() for s in SENTENCE_RE.split(text or '') if s.strip()]
        if len(sentences) <= self.summary_sentences:
            return ' '.join(sentences)

        model = _embedder()
        embedded = model.encode(sentences, normalize_embeddings=True,
                                batch_size=32, show_progress_bar=False)
        document = embedded.mean(axis=0)
        norm = float(document @ document) ** 0.5
        if norm:
            document = document / norm
        central = embedded @ document

        picked: list[int] = []
        for _ in range(self.summary_sentences):
            best, best_score = None, None
            for i in range(len(sentences)):
                if i in picked:
                    continue
                # Penalise a sentence by how much it repeats one already
                # chosen. Three sentences saying the same thing summarise
                # nothing.
                overlap = max((float(embedded[i] @ embedded[j]) for j in picked),
                              default=0.0)
                score = float(central[i]) - 0.5 * overlap
                if best_score is None or score > best_score:
                    best, best_score = i, score
            if best is None:
                break
            picked.append(best)

        return ' '.join(sentences[i] for i in sorted(picked))

    def sentiment(self, text: str) -> float | None:
        """Withheld. See this module's docstring."""
        return None
