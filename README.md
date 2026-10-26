# OSINT-Sentry

An integrated, credibility-first framework for automated OSINT collection,
multimodal misinformation detection, and GIS-based actionable intelligence.

Django REST API backend + Vite/React frontend.

All five analysis modules are built and wired to the console: source
credibility and site safety (1), content fetching, corroboration and
fact-check lookup (2), social graph and campaign detection (3), media
forensics (4), and geolocation and reporting (5) - with audit logging across
every model.

One rule runs through all five, and most of the design follows from it:

> **No rating is given rather than a bad one.**

Every module can decline to answer, and when it declines it says why in the
same place it would have put the number. A failed corroboration search, an
account with too little graph to judge, an image whose JPEG history makes error
level analysis meaningless, a place name the gazetteer does not carry, a
fact-check lookup with no API key - each records *what it could not establish*
rather than a zero. A zero and "nobody looked" are the same glyph on a
dashboard and are not the same finding.

Both trained models follow the same rule. The credibility model is right two
times in three if forced to answer, so it is calibrated and allowed to decline
instead: at its shipped floor it speaks about 11% of domains and is right
96.7% of the time when it does. The content analyzer's entity model drops any
name it is not confident about, trading 1.9 points of recall for 3.2 points of
precision, because an entity the console leaves out costs an analyst less than
one it gets wrong.

## The ML seam

The plan specifies GBT+BERT credibility scoring, spaCy/BERTopic/RAG content
analysis, an RGCN plus RoBERTa for social graph work, and a ViT for media
forensics. **Four of them are measured and shipped: the credibility GBT, the
content analyzer's BERT, the social module's RoBERTa pair, and the media
module's ViT detector.** The RGCN is built and trainable but ships with no
weights, for a reason given below that is about the datasets rather than the
code. Module 5 has nothing to train: the plan's Nominatim is a geocoding web
service, not a model, and the gazetteer that replaces it is a lookup table. The rest are abstract base classes with dependency-free
heuristic implementations behind them, so the pipeline - tasks, gating,
signals, endpoints, admin - is real and testable today.

A fresh clone installs `requirements.txt` alone and all five modules come up,
because nothing in the running platform imports lightgbm or torch and no
default setting points at a model file. The trained engines need
`requirements-ml.txt`; see [The trained credibility model](#the-trained-credibility-model),
[The trained content analyzer](#the-trained-content-analyzer),
[The trained social engines](#the-trained-social-engines) and
[The trained media detector](#the-trained-media-detector).

| Setting | Interface | Default (heuristic) | Plan calls for |
| --- | --- | --- | --- |
| `CREDIBILITY_SCORER` | `credibility.scoring.BaseScorer` | **real** RDAP registration age, TLD, publisher | GBT + BERT - **GBT trained, see below**; no BERT |
| `CONTENT_ANALYZER` | `content.pipeline.BaseAnalyzer` | proper-noun NER, term-frequency topics, lead-sentence summary | spaCy, BERTopic, RAG - **BERT NER trained and measured, see below**; no BERTopic, no RAG |
| `CONTENT_FETCHER` | `content.fetchers.BaseFetcher` | **real** feed-first fetcher over `urllib` | Scrapy spiders |
| `SOCIAL_GRAPH_SCORER` | `social.analysis.BaseGraphScorer` | reciprocity + neighbourhood overlap, abstaining below a floor | RGCN (PyG/DGL) - **built and trainable, ships with no weights, see below** |
| `SOCIAL_TEXT_SCORER` | `social.analysis.BaseTextScorer` | word lexicons, abstaining on no match | RoBERTa - **trained and measured, see below** |
| `SOCIAL_CAMPAIGN_DETECTOR` | `social.campaigns.BaseCampaignDetector` | shared-target overlap + connected components with a common core | graph clustering |
| `MEDIA_FORENSICS_ANALYZER` | `media.forensics.BaseForensics` | **real** JPEG quantisation, EXIF preview, metadata; ELA shown but not scored | ViT deepfake detection - **published ViT measured on its authors' held-out set, see below** |
| `REPORTGIS_GEOCODER` | `reportgis.geocode.BaseGeocoder` | **real** gazetteer compiled into the source; no service, no key | Nominatim / commercial geocoder |
| `REPORTGIS_REPORT_BUILDER` | `reportgis.reporting.BaseReportBuilder` | Markdown assembled from counted figures, never generated prose | LLM narrative generation |
| `CONTENT_FACT_CHECK_CLIENT` | `content.factcheck.BaseFactCheckClient` | null client; **real** Google Fact Check Tools behind an API key | - |

Every heuristic reports its own `model_version` (`heuristic-v1`, etc.) on the
rows it writes, so heuristic output is never mistaken for model output. Swapping
in a real model is a settings change; no call site moves.

## The trained credibility model

A gradient-boosted tree over domain metadata, trained on 9613 domains labelled
from Media Bias/Fact Check's factual-reporting ratings. It ships at
`credibility/artifacts/credibility_gbt.joblib` with its measurements beside it
in `credibility_gbt.metrics.json`.

**It predicts what MBFC would say about a domain.** That is one organisation's
editorial judgement, largely about US outlets, assembled by people and
contested in places. It is not ground truth, and every rationale the scorer
writes says so, because a number on a console acquires authority it has not
earned unless something on the screen keeps saying where it came from.

### Does registration metadata earn its place?

The heuristic docks 25 points from a domain under a month old. That is a
testable claim, so the training command fits three models on the 1191 domains
that have a registration record and reports five-fold cross-validated AUC:

| Features | AUC |
| --- | --- |
| domain string only | 0.580 |
| registration only | 0.799 |
| both | 0.812 |

The assumption holds. Registration carries nearly all the signal, and the
domain string on its own is barely better than a coin - worth knowing before
anyone is tempted to judge a site by how its name reads.

### Why it abstains

On a held-out fifth the model reaches **0.722 AUC** and **67.0% accuracy**
against a **60.5% majority baseline**. A real edge, and a modest one. A softmax
emits a number regardless, so the probability is isotonic-calibrated and the
scorer stays silent below a confidence floor:

| Floor | Speaks on | Right when it speaks |
| --- | --- | --- |
| 0.50 | 100.0% | 67.0% |
| 0.70 | 32.0% | 86.3% |
| 0.80 | 23.8% | 91.5% |
| **0.90** | **11.0%** | **96.7%** |
| 0.95 | 7.5% | 97.2% |

`CREDIBILITY_ML_MIN_CONFIDENCE` defaults to 0.90, chosen from that table rather
than from taste. Forced to answer every time the model is right two times in
three; allowed to decline, it is right nineteen times in twenty and says
nothing about the rest. That is the platform's rule applied to a classifier:
**no rating is given rather than a bad one.**

Coverage is 11% across all domains and 23.8% among those with a registration
record, since a domain whose registry withholds its date gives the model
little to work with.

### Using it

```
pip install -r requirements-ml.txt
```

Then point the setting at one of two scorers:

| Setting value | Behaviour |
| --- | --- |
| `credibility.ml.BlendedScorer` | The heuristic produces the score; a confident model call moves it by `CREDIBILITY_ML_ADJUSTMENT` (default 15). This is the one to deploy. |
| `credibility.ml.GradientBoostedScorer` | The model alone. Returns `UNKNOWN` for the ~89% of domains it is unsure about, which is honest and not much use as the only scorer. |

Fifteen points is deliberate: it sits between "has a named publisher" (+8) and
"has a registration date" (+18/-25), because at the shipped floor the model is
better evidence than the first and weaker than the second.

### Rebuilding it

```
python manage.py build_credibility_dataset --rdap 1400
python manage.py train_credibility_model
```

The first command takes about twenty minutes, fetching RDAP one domain at a
second. It is resumable and caches every response, so an interrupted run
resumes rather than refetching.

`credibility/data/` is not tracked. The MBFC dump is someone else's editorial
work and is not redistributed here, which is why the artifact is committed
though its corpus is not - the same trade a compiled binary makes.

### The one thing to watch

Training and serving must build the same feature row. They share
`credibility/features.py` for exactly that reason, and `credibility/test_ml.py`
compares the two paths **by value**, not just by key. An earlier version
compared keys alone and passed while five of the eight registration columns
were wrong at serving time: the shape was right and only the contents were
blank. That bug cut the model's coverage from 23.8% to 1.4% without failing a
single test.

## The trained content analyzer

Module 2's default analyzer finds entities with a regular expression that
calls any two capitalised words a person. It calls Bayelsa State a person. The
trained analyzer replaces it with `dslim/bert-base-NER`, a BERT fine-tuned on
CoNLL-2003, and uses `all-MiniLM-L6-v2` sentence embeddings for the topics and
the summary.

**What the plan asked for and what shipped are not the same, and the gap is
worth stating.** The plan named spaCy for entities, BERTopic for topics and
RAG for the summary. The entities are a real model doing that job. The topics
are not BERTopic and the summary is not RAG:

| Step | Plan | Shipped | Why |
| --- | --- | --- | --- |
| Entities | spaCy / BERT NER | BERT token classifier, measured | The job as specified. |
| Topics | BERTopic | keyphrases ranked by embedding similarity | BERTopic names clusters it found in a corpus. Fitting it on somebody else's news archive and calling those clusters the subject of today's article would be a worse answer, not a better one. |
| Summary | RAG over a self-hosted LLM | extractive: the sentences closest to the document's own embedding | Nothing here contacts a language model, so nothing here can invent a sentence the article does not contain. |
| Sentiment | - | withheld | Good sentiment models exist and Module 3 uses one, but they are tuned on short social text. Scoring a news article with one and recording the number is the bad rating this platform declines to give. |

### What the entity model is worth

Measured by `manage.py evaluate_ner` on the full CoNLL-2003 test split: 3453
sentences, 5648 gold entities, scored span-exact including the label.

| Floor | Precision | Recall | F1 | Entities kept |
| --- | --- | --- | --- | --- |
| none | 0.9021 | 0.9182 | 0.9101 | 5749 |
| 0.70 | 0.9162 | 0.9115 | 0.9138 | 5619 |
| 0.85 | 0.9281 | 0.9047 | 0.9163 | 5506 |
| **0.90** | **0.9336** | **0.8993** | **0.9161** | **5440** |
| 0.95 | 0.9410 | 0.8892 | 0.9143 | 5337 |
| 0.99 | 0.9542 | 0.8598 | 0.9045 | 5089 |

`CONTENT_NER_MIN_CONFIDENCE` defaults to 0.90. F1 is flat from 0.85 to 0.95,
so the floor is chosen on the other axis: 0.90 buys 3.2 points of precision
for 1.9 points of recall, and an entity the console does not show costs an
analyst less than one it shows wrongly.

The unfiltered 0.9101 matches the 91.3 the model's own card reports, which is
the useful part of running this at all - it says the evaluation harness is
measuring the serving path correctly rather than measuring a bug.

Two caveats the numbers do not carry. CoNLL-2003 is Reuters newswire from 1996
and 1997 and this model was fine-tuned on its training split, so this is a
held-out test set but it is newswire marking newswire; a forum post will do
worse. And the corpus is tokenised, so the command rejoins the tokens into
sentences before the model sees them, which is the shape real input arrives in
and costs a little accuracy around punctuation.

### Why the topic floor is 0.30

There is no gold answer for "the right five keyphrases for this article", so
`manage.py evaluate_topics` measures the thing the floor actually exists to
stop: offering a phrase that is not what the document is about. It scores 300
AG News articles against their own candidate phrases and against phrases
lifted from other articles.

Own phrases average 0.61 for the best and 0.46 for the fifth. Phrases from a
different article average 0.05.

| Floor | Keeps its best topic | Keeps its fifth | Accepts a foreign phrase |
| --- | --- | --- | --- |
| 0.20 | 100% | 100% | 3.5% |
| 0.25 | 100% | 99.7% | 1.2% |
| **0.30** | **100%** | **99.0%** | **0.5%** |
| 0.40 | 100% | 86.7% | 0.06% |

0.30 costs the weakest of five topics on one article in a hundred and halves
the rate at which an unrelated phrase gets through.

Candidates are one to three words, generated inside sentence and clause
boundaries and never across them, and a three-word candidate has to be joined
by a connector rather than a verb. Both rules came from reading the output:
sliding a window over the whole document produced `orders slow shares`, the
end of one sentence welded to the start of the next, and allowing any middle
word produced `warned that cholera`, which is a clause with its subject
removed rather than a name for a topic.

### Using it

```
pip install -r requirements-ml.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
python manage.py fetch_models --module content
```

Then set `CONTENT_ANALYZER=content.ml.TransformerAnalyzer`.

The weights are about 450MB, downloaded on demand into `CONTENT_MODEL_CACHE`
(`.model-cache/`, gitignored) and never committed. `fetch_models`
pulls them ahead of time so the first article analysed after a deploy is not
the one that waits; `--check` reports whether they are already on disk without
reaching the network. Loading is deferred to first use and memoised per
process, so Django starts normally on a machine with no weights at all.

Analysis runs in about 0.2 seconds per article on a CPU after the first load.
The models are not loaded into the Django process in production: analysis
happens in `content.tasks.analyze_content`, which is a Celery task.

### Rebuilding the measurements

```
python manage.py evaluate_ner
python manage.py evaluate_topics
```

Both write to `content/artifacts/`. Both are measurements rather than
training: these two models are published fine-tuned weights, and nothing here
fits them further.

## The trained social engines

Module 3 asks two questions, and the plan names a model for each: RoBERTa for
what a post says, an RGCN for how an account behaves. The text half shipped.
The graph half is built, trainable and tested, and ships with no weights.

That asymmetry is the honest part of this section, so it comes first.

### The RGCN ships with no weights, and why

A bot classifier has to be trained on accounts that somebody looked at and
judged. The public labelled Twitter graphs - TwiBot-20 and the Cresci
collections - are released under access agreements with their authors: you
request them, you say what you will use them for, you agree to terms. Copies
have been re-uploaded to places where no agreement is involved, and training
on one of those would have been an afternoon's work.

Doing that would route around the authors' terms rather than meet them, and
"the file was easy to find" is not an argument that it was mine to use. So
`social.ml.RgcnGraphScorer` is a complete relational graph network - five
relations in both directions, basis decomposition, nineteen behavioural
features, a confidence floor, an artifact format that records what it was
fitted on - with nothing fitted into it.

What it has instead is `SocialAccount.is_automated`, a nullable field an
analyst sets in the admin on accounts they have actually reviewed, and
`manage.py train_graph_model`, which trains on those labels and refuses to
run below twelve of them with four of each class. That is the only labelled
graph this project can honestly claim: yours.

### What the text models are worth

Two published fine-tuned RoBERTa models, measured on TweetEval's held-out
test splits with `manage.py evaluate_text_scorer`. Numbers are in
`social/artifacts/roberta_text.metrics.json`.

| | sentiment | offensive |
| --- | --- | --- |
| split | `sentiment/test`, 12,284 posts | `offensive/test`, 860 posts |
| accuracy | 0.7218 | 0.8593 at the 0.50 mark |
| macro F1 | 0.7240 | 0.8155 |
| ROC AUC | - | 0.9003 |
| majority baseline | 0.4833 | 0.7209 |

The offensive model's AUC is the figure that matters for it, because the
stored field is the probability rather than a verdict: nothing in the
platform thresholds it, so what counts is whether the ranking is
informative, and 0.90 says it is. Its accuracy at 0.50 is printed for
orientation and should not be read as a decision the system makes.

Both numbers land where the model cards put them, which is the point of
running the evaluation at all: it says the serving path is the path that was
measured, rather than something adjacent to it that happens to also return
numbers.

### Why the sentiment floor is 0.70

The sentiment model reads every post three ways. When its best reading is
weak, that is the model declining, not the post being balanced - and a
near-zero score stored for a post the model could not read is
indistinguishable, downstream, from a near-zero score for a post it read as
genuinely neutral.

| floor | speaks on | right when it speaks |
| --- | --- | --- |
| 0.34 | 99.99% | 72.2% |
| 0.50 | 96.0% | 73.2% |
| 0.60 | 81.2% | 76.5% |
| **0.70** | **64.8%** | **80.8%** |
| 0.80 | 46.6% | 85.9% |
| 0.90 | 21.7% | 92.0% |

There is no knee in that curve: every step trades about sixteen points of
coverage for about four of accuracy. So the line is not where the curve
turns, it is where the stored numbers stop being wrong one time in four.
0.70 is the first row above 80%. Below the floor the post is recorded with no
sentiment and a rationale that says the model would not commit, and the
console renders it as *not scored* rather than as zero.

The graph model's floor is higher - 0.80 - for a reason that is not
statistical. Calling an account automated is an accusation about a person.

### What these models do not read

Nothing in this module reads what an account says about itself. No display
name, no biography, no avatar, no follower count on its own. Those are what
an operator controls, and a classifier that reads them learns what a bot
writes in its biography this year rather than how one behaves. The nineteen
features in `social/features.py` are degree, reciprocity, repetition and
per-relation counts - things an operator would have to change their
behaviour to change.

Two caveats the console repeats on every post it scores:

**These are tweet models.** They were fine-tuned on short, informal, English
posts, and a long post, a formal one, or one in another language is outside
what they were trained on.

**"Toxicity" is narrower than it sounds.** The offensive model predicts
whether an annotator would mark a post offensive, which in TweetEval means
open abuse and profanity. A polite smear scores low. A low number is the
absence of the thing the model was taught to find, not the absence of malice.

### Train and serve read one graph

Module 1 shipped a scorer whose serving path built a different feature row
than its training path, five of eight columns were wrong, every test passed
because they compared the column *names*, and the model's coverage fell by a
factor of seventeen without anything failing.

Module 3 is built so that cannot happen quietly:

- `social/features.py` holds the only definition of a feature row, and
  imports nothing from Django.
- `social/graphdata.py` holds the only reader of the account graph. The
  scoring task, the campaign detector and the training command all call it.
- The artifact records `feature_names` and `relations`, and loading one
  whose lists disagree with the running code raises rather than adapts.
- `social/test_ml.py` compares the two paths **by value**, both on a fixture
  and on a graph read out of a real database.

### Using them

```
pip install -r requirements-ml.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
python manage.py fetch_models --module social
```

Then set `SOCIAL_TEXT_SCORER=social.ml.RobertaTextScorer`. The weights are
about 1GB, downloaded into `CONTENT_MODEL_CACHE` (`.model-cache/`,
gitignored) and never committed; `--check` reports whether they are on disk
without reaching the network. Loading is deferred to first use and memoised
per process, so Django starts normally on a machine with no weights.

For the graph model, label accounts in the admin and then:

```
python manage.py train_graph_model
```

It prints the graph it read, the split it held out, the training curve, and
a coverage table like the one above, then writes `social/artifacts/rgcn.pt`
with its metrics beside it. Set `SOCIAL_GRAPH_SCORER=social.ml.RgcnGraphScorer`
to serve it and `SOCIAL_GRAPH_MIN_CONFIDENCE` from that table.

The command will warn, every run, while fewer than 120 accounts are
labelled. That warning is not a nag about dataset size; it is the difference
between a model and a note of what one analyst thought about forty accounts.

### Rebuilding the measurements

```
python manage.py evaluate_text_scorer
```

Writes `social/artifacts/roberta_text.metrics.json`. It is a measurement,
not training: both text models are published fine-tuned weights and nothing
here fits them further. The sentiment split takes about seven minutes on a
CPU.

## The trained media detector

The plan calls for a ViT deepfake detector in Module 4. `media.ml.ViTForensics`
is one, and it was not trained here. This section says why, what was
measured instead, and where the line sits below which it says nothing.

### Whose weights, and why not ours

The weights are the Community Forensics detector (Jeongsoo Park and Andrew
Owens, CVPR 2025): a ViT-Small fine-tuned on 2.7 million images from 4,803
generators, published by its authors under the MIT licence. The paper's
central finding is that a detector learns the generators it was shown rather
than generation in general, and that breadth of generators is what makes one
hold up on new ones. A model trained on a CPU here, on a public benchmark
covering two or three generators, would have been a worse detector than the
one already published and no more honest for being home-made.

The revision is pinned (`MEDIA_DETECTOR_REVISION`). The authors' repository
has replaced its weights once already, to fix a config that split attention
into the wrong number of heads, and a floor measured against one set of
weights says nothing about the next.

### What it was measured on

`manage.py evaluate_detector` scores a stratified sample of the authors'
held-out evaluation set, `OwensLab/CommunityForensics-Eval` (CC BY-NC-SA 4.0):
51,836 real photographs and generated images, from 21 generators that were
held out of the training set. The sample takes the same number of
row groups from every generator and every class, so no single generator
dominates the totals: four row groups of 25 images per generator per class,
166 row groups and 3,930 images in all.

The report covers 3,479 of them: 1,567 photographs and 1,912 generated
images, from all 21 generators. The other 451 were never scored. Hugging
Face was serving the last 19 row groups slowly enough that the run was
stopped there, and the report was built from the scores already cached
(`--cached-only`). The gap is uneven: 374 of the missing images are
photographs from the pairs for IdeogramV1, LCM_lora_ssd1b, DeciDiffusionV2,
LCM_lora_sdxl and the two Midjourney sets, so those generators have few or no
photographs in the per-generator figures. The missing row groups are listed
in the report under `sample.row_groups_missing`.

It is scored twice. Once as published, and once after every image has been
re-saved as a JPEG at quality 85, which is closer to what a picture looks
like after it has been through a messaging app or a social network. The
floor is chosen from the second pass. Numbers are in
`media/artifacts/vit_detector.metrics.json`.

| | as published | re-saved as JPEG, quality 85 |
| --- | --- | --- |
| ROC AUC | 0.9697 | 0.9504 |
| accuracy at the 0.50 mark | 0.8945 | 0.8137 |
| photographs read as photographs | 97.0% | 99.3% |
| generated images read as generated | 83.3% | 66.7% |
| mean average precision over generators | 0.9846 | 0.9602 |

Recompression does not make the detector call photographs generated. It
does the opposite: it misses twice as many of the generated images, a third
of them rather than a sixth, and accuses fewer photographs. The ranking survives (AUC 0.95), but the scores
slide toward *photograph*.

The average hides a wide spread. After recompression, the generators it
reads worst are Imagen3 (0.53 accuracy at 0.50), Hourglass (0.61),
IdeogramV2 (0.62), MidjourneyV5.2 (0.70) and MidjourneyV6.1 (0.71). It
caught only 7 of IdeogramV1's 25 generated images. The best, among them
the LCM variants, stable_cascade and DFGAN, are above 0.95.

### Why the floor is 0.95

From the JPEG pass, over 1,567 photographs and 1,912 generated images:

| floor | speaks on | right when it speaks | "generated" calls right | photographs called generated | generated images caught |
| --- | --- | --- | --- | --- | --- |
| 0.50 | 100% | 81.4% | 99.14% | 11 | 1,275 |
| 0.70 | 95.2% | 83.2% | 99.34% | 8 | 1,207 |
| 0.80 | 92.2% | 84.1% | 99.49% | 6 | 1,163 |
| 0.90 | 87.5% | 85.5% | 99.73% | 3 | 1,093 |
| **0.95** | **82.8%** | **87.2%** | **99.90%** | **1** | **1,029** |
| 0.98 | 77.5% | 89.1% | 100% | 0 | 962 |
| 0.99 | 73.1% | 90.4% | 100% | 0 | 901 |

The floor is read from the JPEG pass, because a picture that reaches an
analyst has usually been re-encoded at least once on the way.

The column it is chosen on is *photographs called generated*. A reading of
*generated* is the only one that changes a verdict, so a confident wrong one
is the detector accusing a real photograph. At 0.90 that happened to three
of 1,567. At 0.95 it happened to one, and the detector still catches 1,029
of the 1,912 generated images. 0.98 accuses none, but in a sample this size
the gap between one and none is not a measurement, and the higher floor
gives up another 67 generated images to get there.

Below the floor the result records that the detector would not commit, with
its reading, and nothing else changes. Above it, a reading of *photograph*
is recorded and changes nothing, because a detector that has not seen a
generator misses it, and "the model found nothing" is not evidence that a
picture is real. A reading of *generated* adds 0.45 to the suspicion and
raises the verdict to `suspicious`. It never raises it to `manipulated`:
that word means the file contradicts its own preview, and a generated image
was not edited, it was made.

### What it does not read

**The edges of the frame.** The model resizes the shortest side to 440
pixels and reads a 384 pixel square from the centre, as it was trained. A
generated object pasted into the corner of a real photograph is outside what
it looks at.

**Which part.** It is a whole-image generation detector, not a splice
detector. It cannot say which region, if any, was photographed, and the
rationale says so on every reading.

**Generators released after its training set.** The evaluation set's
generators were held out from training, which is the best available proxy
for that, and the per-generator figures above show how uneven the result is
even there.

Everything in Module 4's file inspection still runs underneath it: the
quantisation tables, the EXIF preview check, error levels and the metadata.

### Using it

```
pip install -r requirements-ml.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
python manage.py fetch_models --module media
```

Then set `MEDIA_FORENSICS_ANALYZER=media.ml.ViTForensics`. The weights are
about 85MB, downloaded into `CONTENT_MODEL_CACHE` (`.model-cache/`,
gitignored) and never committed. Loading is deferred to the first image and
memoised per process. The console's Media page marks the engine as trained
when this class is configured.

### Rebuilding the measurement

```
python manage.py evaluate_detector
```

Reads only the columns it needs from the dataset's parquet files over the
Hugging Face hub, caches each row group's scores under `.model-cache/`, and
writes `media/artifacts/vit_detector.metrics.json`. Almost all of the
running time is downloading images. Scoring is quick on a CPU, and the
147 row groups behind the numbers above took most of an evening over a home
connection. `--groups` widens the sample and `--workers` sets the number of
parallel downloads.

A row group that fails to download is reported and left out. Running the
command again downloads only the row groups that are still missing.
`--cached-only` writes the report from what is already scored, lists the
rest as missing, and downloads nothing.

## Module 1: is the site itself safe? - a second rating out of 10

The credibility score reads registration metadata. That says nothing about
what the site *serves*. **Inspect site** fetches the homepage, opens a TLS
connection, and reads the certificate, the response headers and the markup,
then rates out of 10 how safe and how cluttered the site is - SAFE, MOSTLY
SAFE, SOME CONCERNS, CAUTION, UNSAFE - with every deduction shown.

Real results, same run:

| Site | Rating | Why |
| --- | --- | --- |
| theguardian.com | **9.8 SAFE** | full security headers, one tracker |
| hindustantimes.com | **6.7 SOME CONCERNS** | 7 ad networks, 5 trackers, 3 headers missing |

and the second one names them: Google Ad Manager, AdSense, Taboola, Criteo,
Amazon Ads, Xandr, 33Across; Chartbeat, Comscore, Google Analytics.

**This is deliberately not folded into the credibility score.** The two answer
different questions - "is this publisher reliable" versus "is this site safe
to open" - and a reputable outlet buried in advertising should show both
facts rather than one number that splits the difference. The credibility
score's documented ceiling and provenance hash stay untouched.

### What it checks

- **Transport** - HTTPS, whether plain HTTP is upgraded, and the certificate:
  issuer, protocol, days to expiry, and *why* it is invalid when it is
  (Python's own verification message, which is already in plain English).
- **What the site declares** - HSTS, CSP, X-Content-Type-Options, clickjacking
  protection, Referrer-Policy. Absence is not an attack; a site with none of
  them is not paying attention.
- **Monetisation and surveillance** - ad networks, aggressive pop-under
  networks, trackers, session recorders, push-notification nagware, and
  in-browser crypto miners, matched against a plain readable list in
  `credibility/trackers.py` so a finding can be argued with. Inline snippets
  (`gtag(`, `fbq(`) are caught even when the script host is self-hosted.
- **Clutter** - pop-up and interstitial containers, auto-playing media,
  embedded frames, meta-refresh redirects, third-party resource volume.
- **Mixed content and insecure forms**, including the one thing that overrides
  everything else.

### Three judgements worth knowing about

**A password field posting over plaintext HTTP caps the rating at 3/10**, no
matter how tidy the rest of the site is. That is not a hygiene issue.

**A cryptocurrency miner is the single heaviest finding (−4).** Ads are
reported and lightly penalised because advertising is normal commercial
publishing; a miner is not a monetisation choice anyone discloses.

**Unreachable is not unsafe.** A site that will not answer gets *no rating*
rather than a bad one - the same rule as a failed registry lookup, and the
same rule the corroboration engine follows.

### Clearing the safety log

**Clear log** on the Site safety panel empties the view. It sets a `cleared_at`
flag rather than deleting rows, so:

- nothing is destroyed - **Show cleared** reads the archive back, and
  **Restore** returns it to the live log;
- the **Assessment log** is a different table and is not touched, so a site
  whose inspections were cleared still has every credibility assessment it
  ever had;
- the site is still a tracked source, still scored, still crawlable.

An analyst emptying a view is saying "I have read these", not "destroy the
evidence" and certainly not "forget the site" - so clearing costs nothing but
the view.

### Two things it is not

It is **not a malware scanner**. It does not detect exploit kits, phishing, or
a compromised page; 9/10 means "serves you little junk", not "safe".

It **does not execute JavaScript**, so a site that injects its advertising at
runtime is undercounted. That is why the presence of a tag manager is itself
reported, and why the caveat appears on every single result rather than in a
footnote.

## Submitting a website for scoring

The **Check a website** box on the Credibility page takes whatever you have -
a pasted article URL, a copied address bar, or a bare hostname. It is reduced
server-side to the domain a registry will answer for, so all of these are the
same source:

```
https://www.bbc.co.uk/news/uk-politics-12345?utm_source=twitter
news.bbc.co.uk
BBC.CO.UK.
bbc.co.uk
```

Normalising happens in `core.domains`, not in the console, so the API, the
admin, and a shell one-liner cannot disagree about what `bbc.co.uk` is.
Submitting a site that is already tracked re-scores it rather than returning a
uniqueness error - someone pasting a link is asking "check this", and "you
already added that" answers a question they did not ask.

Inputs that name no domain are rejected with the reason: an IP address (no
registration record exists to look up), a `mailto:`/`javascript:` scheme,
something with no domain suffix, or illegal characters.

A pasted `https://` link is also recorded as the source's URL, since it is
direct evidence the site serves TLS. Without one, transport reads as
*unknown* and costs nothing - only an observed plaintext URL is penalised.

## Module 1: what the credibility score actually measures

Module 1 is no longer a placeholder. It reads **real domain registration data
over RDAP** - the registries' own JSON-over-HTTP successor to WHOIS. No API
key, no scraping, no third-party dependency, just `urllib`.

That matters because the previous version faked it. It computed "domain age"
from `Source.first_seen`, which is how long *this database* had known about
the source, so a 30-year-old newspaper added today was penalised as new,
while any row that had sat in the table for a year drifted upward. `heuristic-v2` fixes the
inversion:

| Case | v1 | v2 |
| --- | --- | --- |
| bbc.co.uk, added today | −15 "new" | +18, registered 1994 |
| Domain registered 5 days ago, in our DB a year | +15 "established" | −25 |

Other deliberate choices:

- **HTTPS earns nothing.** It is free, automatic, and universal - every phishing
  site has it - so its presence is not evidence. An observed *plaintext* URL
  still costs 10; a source with no URL recorded is scored as unknown, not as
  insecure.
- **"Known to us" is recorded but never scored.** Rewarding it is circular. It
  stays in `SourceFeatures` so the provenance hash covers it, at weight zero.
- **Metadata cannot reach VERIFIED.** The score is capped at 82; the tier needs
  85. No amount of clean WHOIS makes a publisher accurate, and the ceiling says
  so structurally rather than in a comment.
- **A failed lookup leaves age unknown, never "new."** An unreachable registry
  must not be able to condemn a domain. Unresolved lookups retry on the nightly
  sweep, because only an `ok` status counts as fresh.

Network I/O lives solely in `credibility/registry.py` and is cached onto
`Source` (30-day TTL) by `refresh_domain_registration`, so scoring itself makes
no network call and stays fast enough to run inside a request.

Set `CREDIBILITY_REGISTRY_CLIENT=credibility.registry.NullRegistryClient` to
run fully offline.

## Module 2: reading the web without being a nuisance

Module 2 is the part that actually goes and gets things. Before it existed the
pipeline was real but starved - `NullFetcher` meant nothing was ever ingested
unless a human POSTed it.

It is **feed-first**. For each source it finds the RSS/Atom feed the publisher
already maintains, reads it, and fetches only the entries not already stored.
That is one request per new article rather than a crawl, and the publisher
decides what is in scope. Feed discovery tries, in order: a `feed_url` set on
the source, the `<link rel="alternate">` the page advertises, then a short list
of conventional paths.

Two things run before any request goes out:

- **The credibility gate, first.** A source below `CONTENT_MIN_CREDIBILITY`
  is never contacted at all. Letting the gate reject items *after* fetching
  would mean spending someone else's bandwidth on documents we intend to
  discard.
- **robots.txt, on every hop.** Including redirect targets - which is why
  redirects are followed by hand rather than by `urllib`. A site that could
  redirect us past its own rules would make the check decorative.

When robots.txt cannot be read, the answer is **no**:

| robots.txt response | Behaviour |
| --- | --- |
| 2xx | parsed and obeyed, `Crawl-delay` included |
| 4xx | no file exists, so nothing is disallowed |
| 5xx, timeout, DNS failure | **disallow everything** |

"We could not ask permission" is not "permission granted", and failing open
there would crawl a site hardest exactly while it is struggling.

The other limits are in `content/http.py` and are limits rather than tuning
knobs: an identifying User-Agent with a contact address, a 2 MB cap applied to
the *stream* rather than to the `Content-Length` a server claims, a
content-type allowlist, `Accept-Encoding: identity` so there is no compression
bomb behind the cap, and a per-host minimum interval that yields to a longer
`Crawl-delay`.

**Hostnames resolving to private, loopback, or link-local addresses are
refused.** A submitted domain is attacker-controlled input; without that check
a link pointing at `169.254.169.254` turns this fetcher into a proxy into the
machine it runs on. The check is on every hop, so a redirect cannot smuggle
one in. It resolves and then connects, so a determined DNS-rebinding attack
still has a window - closing that needs connecting to the pinned address,
which `urllib` does not expose.

Extraction is `html.parser`, not Scrapy: strip the furniture, prefer text
inside `<article>`, fall back to paragraphs, then to the whole body. Metadata
comes from OpenGraph, JSON-LD, and `article:*` before `<title>`. Storage is
keyed on the page's **canonical URL**, so campaign parameters do not store one
article five times. There is no JavaScript engine - a page rendered entirely
client-side yields nothing, and says so rather than storing an empty body.

Feeds are parsed with `xml.etree`, which is documented as vulnerable to entity
expansion. Every such attack needs a document type declaration, so a feed
containing `<!DOCTYPE` or `<!ENTITY` is refused before the parser sees it. No
real feed has one, so this costs nothing and avoids a dependency.

A source found to have no feed is left alone for a day
(`CONTENT_FEED_RETRY_AFTER`) rather than re-probing every conventional path
every hour. Clicking **Crawl feed** in the console overrides that, since the
back-off exists to restrain the schedule, not the analyst.

Set `CONTENT_FETCHER=content.fetchers.NullFetcher` to contact nobody - the
same switch `NullRegistryClient` gives Module 1.

## Cross-referencing a story: the 0-10 rating

Reading a page is only half the question. The other half is *does anyone else
say this*. Every stored article can be checked against other outlets, which
produces a rating out of 10 and a tag - TRUSTWORTHY, LIKELY RELIABLE,
PARTIALLY CORROBORATED, UNCORROBORATED, DISPUTED BY OTHERS, DO NOT TRUST -
logged newest-first in the console. Reading a page runs the check
automatically.

**This measures corroboration, not truth.** It asks whether independent
outlets carried the same story, and whether the specifics this article states
match what they printed. A widely repeated falsehood corroborates beautifully;
a genuine scoop does not corroborate at all until everyone catches up. The tag
is a summary of that measurement and is not a fact-check, so every report
stores the keywords it searched, the outlets it found, their similarity, the
figures it compared, and the arithmetic that produced the number.

The rating is a weighted sum of three things, each returned as its own reason
with its signed contribution, so the console can show a column that visibly
adds up to the score:

| Component | Weight | What it measures |
| --- | --- | --- |
| Who published it | 0.30 | the source's Module 1 credibility |
| Who else reported it | 0.45 | independent outlets carrying a matching story |
| Do its details match | 0.25 | the article's own figures, found in their text |

When an article states nothing checkable, the third share passes to the second
rather than being lost - otherwise a story with no numbers in it could not
score above 7.5 however many outlets confirmed it. A conflicting figure costs
1.5, capped at 3.0, so one contested number cannot drive a story to zero.

### Where it looks

Four backends run together: what we have already ingested, the **live feeds of
other tracked outlets**, a standing **panel of major newsrooms** in
`content/reference_outlets.py` - consulted whether or not anyone registered
them as sources - and **GDELT's DOC 2.0 API**, a free, key-less index of
global news chosen for the same reason Module 1 uses RDAP.

The reference panel exists because without it a fresh install can only
cross-reference against the one or two feeds someone has added, and reports
every story as uncorroborated: truthfully, but having looked nowhere. Nothing
on that list is treated as authoritative - an outlet we do not track is scored
at neutral credibility like any other unknown domain, so being on it buys a
match nothing beyond being looked at.

The best few candidates are then **fetched and parsed in full**, through the
same robots-respecting, rate-limited fetcher as everything else, and matched on
their actual text rather than a nine-word headline. `CORROBORATION_DEEP_READS`
(default 5) sets how many; it is the main thing an analyst waits for.

### Checking what the article actually says

`content/claims.py` pulls the article's checkable specifics - figures, sums,
percentages, dates - and looks for those same specifics in the text of the
outlets that were read. Each one comes back `agreed`, `disputed`, or
`unverified`, never `true` or `false`: every outlet copying one wrong figure
off one wire report agrees perfectly, and a disputed figure may be a correction
rather than an error.

A false dispute is worse than a missed one, because it accuses a correct
article of getting a fact wrong. So a conflict is only reported when the other
outlet is demonstrably discussing the same quantity: two or more of the claim's
context words present, the rival figure within a short span of them, the same
kind of quantity (money is never compared against percentages), the same
magnitude, and the same counted noun - 47 *people* is only ever contradicted by
another count of people. Every one of those guards is there because its absence
produced a wrong answer on a live article.

### Fact-checks somebody else already did

Everything above is a machine reading text. This part is different: it looks up
work a *person* did. `content/factcheck.py` queries the Google Fact Check Tools
API, which indexes the ClaimReview markup published by IFCN signatories - Full
Fact, PolitiFact, Snopes, AFP, Boom Live, Africa Check and several hundred
others. What comes back is a citation, and a citation is the one output of this
platform that needs no defending.

**It is off by default and needs an API key you supply yourself.** Nothing in
this repo holds one, and a fresh clone runs every check without one:

```
CONTENT_FACT_CHECK_CLIENT=content.factcheck.GoogleFactCheckClient
CONTENT_FACT_CHECK_API_KEY=your-key
```

Enable the Fact Check Tools API on a Google Cloud project and create a key for
it. Without the key the client reports **`no_key`** and makes no request at
all - an unauthenticated call returns 403, and a 403 arriving as "lookup
failed" is a more alarming thing on screen than a feature nobody has
configured.

**A fact-check never moves the rating, in either direction.** This is the rule
the layer is built around, and it is a restriction rather than a shortcut. The
lookup is matched on the article's search terms, so a hit means *somebody has
written about something like this* - and a query about flooding in Kerala will
happily return a fact-check of a different claim about flooding in Kerala. A
number that shifts on a keyword overlap is a guess with a decimal point on it,
and this module has already shipped one of those. So the working line for the
fact-check carries no contribution at all, and the console says the rating is
unaffected, because a reader who assumes the number already accounts for a
fact-checker's ruling will quietly discount it twice.

What the analyst gets instead is the publisher, the claim as the fact-checker
stated it, their verdict in their own words, and a link - enough to settle the
question in about ten seconds, by reading, which is the correct way to settle
it.

Three smaller decisions follow from the same caution:

* **Close and loose matches are labelled and never merged.** A close match
  carries most of the article's search terms; a loose one is shown dimmed as a
  lead to follow. Only close matches are counted as negative, because a warning
  colour on a keyword coincidence is worse than no panel at all. Below two
  shared terms nothing is reported - one shared proper noun is a coincidence,
  and coincidences printed beside the word FALSE are how a console libels
  someone.
* **The publisher's own wording always survives.** `textualRating` is free text
  a fact-checker chose. "Pants on Fire", "Four Pinocchios" and "Altered photo"
  all bucket to *false* here, and all three mean something more specific than
  that. The bucket only decides which rows get emphasis; the words are what is
  displayed. Anything unrecognised stays *unrated* rather than being guessed
  towards false.
* **Five states, not two.** *Switched off*, *no key*, *lookup failed*, *nothing
  published*, and *matches found* all render as an empty list, and four of them
  are not findings about the article. They are stored and shown separately.

What it cannot do: tell you an unchecked claim is true. The overwhelming
majority of news has never been fact-checked by anyone, so absence here means
absence and nothing else. It also only sees fact-checks published with
ClaimReview markup - a fact-check that exists but was never marked up does not
exist to this layer.

### Why every article used to score 3.1

Worth recording, because the arithmetic was never the problem. Almost every
article came back rated 3.1/10 - which is exactly `0.45 × (68 ÷ 10)`, the
source-credibility half of the old formula with corroboration contributing
precisely zero, on story after story.

The cause was the query. Search terms were ranked by how often each proper noun
appeared *anywhere on the fetched page*, so navigation menus and sidebar promos
outvoted the story. An article headlined "RBI intervention, dollar flows push
Indian rupee to two-month peak" was searched for as **`Indian, Budget, share,
price`** - not one word of which is in the headline - and the 139 unrelated
articles that came back were then correctly rejected, one at a time, down to
zero.

Terms are now ranked by *where* a word appears rather than how often, because
position is the signal that survives scraping: page furniture repeats endlessly
and sits everywhere, while a story's subject is in its headline. A term the
site prints on every article it publishes is recognised as furniture and
discounted, weekdays and months are discarded outright, and how often the
article returns to a headline word separates the subject from the sub-editor -
"floods" recurs throughout a flooding story, "spotlights" appears once. The same
article now searches `Indian, RBI, intervention`, and the Ukraine story that
scored 3.1 scores 9.0 on eight independent outlets.

### The three rules that keep it honest

**A failed search is not a negative result.** GDELT rate-limits hard. If the
lookup does not complete, the report carries no rating and says why. An early
version got this wrong in a way worth recording: `CompositeSearch` reported
itself available whenever *any* backend answered, and since the stored-item
backend never fails and usually finds nothing, a rate-limited GDELT became a
confident "3.4/10, uncorroborated" on a story that had never actually been
cross-referenced. Availability is now asymmetric - finding corroboration is
valid even if part of the search failed, but finding *none* is only meaningful
if we looked everywhere.

**Syndication is not corroboration.** Forty mastheads running one wire story
is one confirmation, not forty. Near-identical headlines collapse into a
single group before anything is counted, as do repeated follow-ups from one
outlet.

**Silence is not guilt.** An outlet Module 1 rates well being first to report
something is tagged UNCORROBORATED, never DO NOT TRUST. That tag is reserved
for a source Module 1 has already condemned - which conversely is not
rehabilitated by a handful of keyword matches.

Similarity is measured against the article's five strongest terms rather than
all of them: most candidates are known only by their headline, and a headline
cannot carry a dozen terms. Dividing by all of them scored genuinely matching
coverage at 25% and discarded it. Once a candidate has been read in full its
body is matched too, which is both fairer and stricter - an outlet sharing one
headline word but never mentioning the subject stops scoring, and one covering
the story properly under a cryptic headline starts.

Set `CORROBORATION_SEARCH=content.corroborate.NullSearch` to disable it. That
backend reports itself *unavailable* rather than empty, so switching it off
can never be mistaken for evidence that nothing corroborates a story.

Note on what is deliberately absent: Google News RSS search would widen this
considerably, and Google's robots.txt disallows `/rss/search`. Module 2 does
not get to obey robots.txt only when convenient.

### Testing it

`content/tests.py` is 135 tests, and `credibility/tests.py` a further 27 -
and **none of them touch the network**: everything outbound is behind
`content.http.request`, a search backend, or a fact-check client, so the tests
replace those and drive the whole pipeline on canned data.

```bash
.venv/Scripts/python.exe manage.py test content
```

They cover the cases that are hard to check by eye - robots.txt returning 503,
a redirect pointing at `127.0.0.1`, a feed declaring a DOCTYPE, an oversized
body, a page that is mostly navigation, a low-scoring source proving it is
never contacted, a partial search refusing to condemn, eight syndicated copies
counting once, and a year-old story failing to corroborate a new one.

They also pin the corroboration bugs that only showed up against live sites: a
navigation bar outvoting a headline, a clock time (`03:12`) surfacing as two
disputed statistics, a year being compared against a death toll, a publication
timestamp treated as a claim, and a line break making a story about 47 *people*
record itself as counting 47 *officials*.

## Module 3: which accounts are acting together?

`social/` reads the shape of the interaction graph and nothing else - no
profile text, no follower counts, no outside service. Two scorers and a
detector, all abstaining.

The trained engines for this module, what they were measured at, and why the
graph model ships with no weights, are in
[The trained social engines](#the-trained-social-engines). Everything below
describes the pipeline they plug into, which runs the same way with the
heuristics a fresh clone starts on.

**Both scorers can decline.** They work by counting, and counting always
produces a number; the question is whether the number is worth anything. One
unanswered mention is 0% reciprocity and a bot likelihood of 1.00. Two accounts
that each mentioned a single newspaper have a target overlap of 1.00. A post
containing no word from the lexicon balances to a sentiment of exactly 0.00 and
files itself beside a weather report, whatever it actually said. All four of
those now record **no score** and a reason saying why, below
`SOCIAL_MIN_INTERACTIONS` and `SOCIAL_MIN_SHARED_TARGETS` (both 3).

The trained engines decline on the same principle and from a measured floor
rather than a counted one: the RoBERTa sentiment model stays silent below
`SOCIAL_TEXT_MIN_CONFIDENCE` (0.70) and the graph network below
`SOCIAL_GRAPH_MIN_CONFIDENCE` (0.80).

The console draws an unscored account **hollow** in the graph and writes *"n
accounts carry no score - too little graph to judge on, not a clean bill of
health"* beside the flag count. A null that renders as green is an account the
scorer refused to judge, shown in the colour of one it cleared.

**Campaign detection** (`social/campaigns.py`) answers the question a
per-account coordination score cannot: not *does anyone else hit the same
targets as this account*, but *who are the five of them*. A campaign is a group
of accounts that all interact with the same three or more accounts. Two rules
keep it from flattering itself:

* **A cluster must have a common core.** Linking accounts pairwise and taking
  the connected components lets A join C through B with nothing in common, and
  a long enough chain eventually swallows the whole graph into one "campaign".
  A component whose members do not *all* share enough targets is refused.
* **What is refused is recorded.** `CampaignRun` logs every pass, including the
  ones that found nothing, with notes explaining what was examined and where it
  stopped. An empty campaign list and a detector nobody has run look identical
  otherwise, and only one of them means there is nothing there.

A campaign is keyed by its membership, so re-running updates the row rather
than stacking a second copy, and a cluster a later pass does not find again is
kept and marked stale rather than deleted - the graph changing underneath it is
a fact about the capture.

**Nothing here asserts intent**, and every cluster carries the line saying so.
A press pack on one beat, a fan community around one account, and a paid
amplification network are the same picture from this angle. The cluster is
where an analyst starts looking, not what they conclude.

```bash
.venv/Scripts/python.exe manage.py seed_social_demo          # a synthetic graph
.venv/Scripts/python.exe manage.py seed_social_demo --clear  # and remove it
```

The demo graph is `demo_`-prefixed and exercises all four outcomes at once: a
ring that is reported, a chain that is refused, a reciprocated group that
scores low, and accounts too thin to judge.

## Module 4: has this image been altered?

`media/forensics.py` is forensics on Pillow alone - no model, no service. It
reads the JPEG quantisation tables, the metadata, the embedded EXIF preview
and the dimensions, and it runs error level analysis.

**Error level analysis is measured, shown, and never scored.** ELA compares
an image against a recompression of itself, on the theory that a pasted-in
region carries its own compression history. Swept over 256 test composites
and 64 untouched frames, the strongest formulation here flagged originals
about as often as forgeries, so the number appears in the result for an
analyst to look at and adds nothing to the verdict. Where its assumptions do
not hold at all - a PNG, or a file already stored at the quality the pass
re-encodes at - the result says which assumption failed instead of producing
a number.

**`manipulated` is reserved for one finding.** A camera writes a small
preview alongside the image, and when an editor forgets to rewrite it, the
preview still shows the original picture. That contradiction is the only
thing allowed to conclude; everything else, however much of it piles up,
tops out at `suspicious`, because every other signal also has an innocent
explanation. Every finding carries its points as `{label, points}`, so a
verdict is a sum an analyst can argue with rather than an assertion.

The trained generated-image detector sits on top of this inspection rather
than replacing it; see [The trained media detector](#the-trained-media-detector).

## Module 5: where did this happen, and what does the report say?

`reportgis/` runs with **no spatial database, no GDAL, and no geocoding
service**. `GeoEvent` stores plain latitude and longitude floats, and
`reportgis/gazetteer.py` is a place-name table compiled into the source. A name
the table does not carry is reported as *not found*; it is never sent anywhere.
That is why every module reports online on a machine with nothing installed -
the PostGIS `PointField` this model used to carry kept the whole app out of
`INSTALLED_APPS` and drew an OFFLINE panel on the console.

Extraction reads the entities Module 2 already found, so geolocation is a
downstream reading of an analysed article rather than a second pass over its
text. Each event records how it was placed - `located` or `limits` - and
`ExtractionRun` logs every pass, including the ones that placed nothing, for
the same reason `CampaignRun` does.

**Reports are assembled, not generated.** `reportgis/reporting.py` builds
Markdown out of counted figures - no prose is invented, and there is no model
in the path. Every report carries a **Gaps** section listing what it cannot
show: the articles that yielded no location, the events placed only to a
country, the window that was empty. A report whose limitations are in the
report is one an analyst can hand to somebody else.

## Layout

```
backend/          Django project (settings split into base/dev/prod)
  settings/       base.py, dev.py, prod.py - configured via .env
  celery.py       Celery app instance
core/             Source model, DRF router, shared exception handler, tasks
  templates/public/  the landing page and privacy policy, plain HTML
  static/favicon.svg the site icon, shared by both public pages
  management/commands/devtoken.py  a console JWT, DEBUG only
accounts/         Role groups (Analyst / Reviewer / Admin) as a data migration
credibility/      CredibilityScore, SiteInspection
  registry.py     RDAP domain registration lookups
  scoring.py      registration-metadata credibility score
  features.py     the one definition of a feature row, shared by train+serve
  ml.py           the trained GBT scorer and the blended one; abstains
  artifacts/      the trained model and its measurements
  management/commands/  build_credibility_dataset, train_credibility_model
  inspection.py   fetch a site and rate its safety/clutter out of 10
  trackers.py     the ad, tracker and miner catalogue, kept readable
content/          ContentItem, and the fetcher that fills it
  http.py         the only outbound calls: limits, rate limiting, host checks
  robots.py       robots.txt, cached per host, failing closed
  extract.py      HTML -> article, via html.parser
  feeds.py        RSS/Atom
  fetchers.py     feed-first WebFetcher; NullFetcher is the offline switch
  pipeline.py     NER / topics / summary (heuristic stand-in)
  corroborate.py  search + deep-read other outlets -> a 0-10 rating
  claims.py       the article's own figures, checked against their text
  factcheck.py    published fact-checks, key-gated; null client by default
  reference_outlets.py  newsrooms consulted whether or not they are Sources
social/           SocialAccount, InteractionEdge, SocialPost, Campaign
  analysis.py     reciprocity and overlap scoring; abstains below its floors
  campaigns.py    clusters of accounts hitting the same targets, and CampaignRun
  management/commands/seed_social_demo.py  a synthetic graph for the console
media/            MediaItem, TamperingResult
  forensics.py    ELA, JPEG quantisation tables, metadata; Pillow only
reportgis/        GeoEvent, Report, ExtractionRun - plain lat/lon, no GDAL
  gazetteer.py    the place-name table, compiled into the source
  geocode.py      name -> coordinates, reporting what it could not place
  reporting.py    Markdown reports assembled from counted figures
security/         AuditLogEntry
frontend/         Vite + React analyst console, proxies /api to Django on :8000
  src/api.js      JWT client with transparent token refresh
  src/theme.css   console theme and cursor styling
  src/components/ Cursor, Panel, Login, shared table primitives
  src/modules/    one view per module, plus the overview
```

## The console

`frontend/` is a JWT-authenticated analyst console covering all five modules.
Log in with a superuser account; the token is exchanged at `/api/auth/token/`
and refreshed automatically when the 30-minute access token expires.

The interface is pointer-reactive throughout: a custom two-part cursor with
viewport crosshairs, a spotlight that unmasks the background grid as it moves,
per-panel sheen that tracks across each card, and a social graph whose nodes
label themselves and trace their edges as the pointer nears them. Cursor
position is written straight to the DOM in a `requestAnimationFrame` loop
rather than React state, since `mousemove` fires far faster than a component
should re-render.

Visual design is an original take on a high-contrast cyberpunk console -
acid yellow on near-black, angular clipped panels, cyan/magenta accents. No
third-party assets, fonts, or marks are used, and there are **no runtime npm
dependencies beyond React** - the graph and map are hand-rolled SVG.

Anyone who has asked their system for reduced motion gets a static, fully
legible console: the glitch, scanline, and cursor effects switch off.

## Prerequisites

Only Python 3.11 and Node.js. There is deliberately **no server to install**
for local work: the database is SQLite, Celery uses kombu's filesystem
transport with results stored in the database, geolocation resolves against a
table compiled into the source, and image forensics needs nothing beyond
Pillow. No module requires an API key to run; the one optional key - for
published fact-checks - leaves its layer reporting *switched off* rather than
failing when it is absent.

## Setup

```bash
cp .env.example .env      # then set SECRET_KEY
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py createsuperuser
```

That is the whole install. `requirements-ml.txt` is separate and optional - it
is needed only to train or serve the credibility model, and nothing in the
running platform imports it.

## Switching to Postgres / Redis

Everything above is env-driven, so moving to real infrastructure is a `.env`
change plus the installs:

| Setting | Local default | Production |
| --- | --- | --- |
| `DB_BACKEND` | `sqlite` | `postgres` (+ `DB_NAME`/`DB_USER`/`DB_PASSWORD`) |
| `CELERY_BROKER_URL` | `filesystem://` | `redis://localhost:6379/0` |

```sql
CREATE DATABASE osint_sentry;
```

**There is no `USE_GIS` switch and no PostGIS requirement.** There used to be
both. `GeoEvent` stored a PostGIS `PointField`, which meant `django.contrib.gis`
had to be importable, which meant a machine without GDAL dropped the entire
`reportgis` app out of `INSTALLED_APPS` - and the console drew Module 5 as
OFFLINE on every machine that had not installed OSGeo4W. The geometry is two
float columns now and the app loads everywhere, which is the right trade for a
platform whose spatial work is "put a dot on a map".

If you later need real spatial queries - distance, containment, projections -
that is the point to bring PostGIS back, and it is a migration on two columns
rather than a rewrite.

## Running

```bash
.venv/Scripts/python.exe manage.py runserver
```

```bash
cd frontend && npm run dev
```

**The console is <http://localhost:5173> - not `:8000`.** Django serves the API
and the two public pages; `:8000/` is the landing page, which points here. The
machine-readable endpoint map moved to `/api/index/` when the landing page took
the root URL. The Vite port is pinned with `strictPort`, so if 5173 is taken the
dev server fails loudly instead of quietly moving to 5174.

| URL | What it is |
| --- | --- |
| <http://localhost:5173> | the analyst console, behind a login |
| <http://localhost:8000/> | the public landing page |
| <http://localhost:8000/privacy/> | the privacy policy |
| <http://localhost:8000/admin/> | Django admin |

There is no sign-up. Accounts are created by the operator with
`manage.py createsuperuser`. For a console token without typing a password into
a form, `manage.py devtoken` prints a JWT pair and the two `localStorage` lines
that install it; it refuses to run with `DEBUG` off.

```bash
.venv/Scripts/celery.exe -A backend worker -l info --pool=solo
```

`--pool=solo` is needed on Windows; the default prefork pool does not work there.

Smoke-test the worker:

```bash
.venv/Scripts/python.exe -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings.dev');django.setup();from core.tasks import add;print(add.delay(2,2).get(timeout=30))"
```

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /` | none | public landing page |
| `GET /privacy/` | none | privacy policy |
| `GET /api/index/` | none | API index - endpoint map and the console's address |
| `GET /api/health/` | none | liveness check |
| `POST /api/auth/token/` | none | obtain JWT access/refresh pair |
| `POST /api/auth/token/refresh/` | none | refresh access token |
| `GET /api/sources/` | JWT | list tracked sources |
| `POST /api/sources/` | JWT | submit a site by URL or domain; 200 + rescore if already tracked |
| `GET /api/credibility/` `.../{id}/` | JWT | scores with tier, rationale, provenance hash |
| `POST /api/credibility/score/` | JWT | request a score (`wait: true` runs inline) |
| `POST /api/credibility/inspect/` | JWT | inspect a site; returns a 0-10 safety rating and its evidence |
| `GET /api/credibility/inspections/` | JWT | the log of site inspections, newest first (`?cleared=1` for the archive, `?cleared=all` for both) |
| `POST /api/credibility/inspections/clear/` | JWT | hide the safety log; optional `source` clears one site |
| `POST /api/credibility/inspections/restore/` | JWT | undo a clear |
| `GET /api/content/` `.../{id}/` | JWT | content with entities, topics, summary |
| `GET /api/content/summary/` | JWT | corpus-wide counts, incl. `unchecked`; counted in the DB, not from the page |
| `POST /api/content/fetch/` | JWT | read a live page (`url`) or crawl a source's feed (`source`) |
| `POST /api/content/{id}/corroborate/` | JWT | cross-reference a story; returns a 0-10 rating, its evidence, and any published fact-checks |
| `GET /api/content/corroborations/` | JWT | the log of credibility checks, newest first |
| `POST /api/content/ingest/` | JWT | submit a document you already have; 409 if the gate rejects it |
| `POST /api/content/{id}/analyze/` | JWT | re-run analysis |
| `GET /api/social/accounts/` | JWT | accounts with bot-likelihood and coordination scores; `null` where the scorer declined |
| `GET /api/social/accounts/summary/` | JWT | counts incl. `accounts_unscored`; counted in the DB, not from the page |
| `GET /api/social/accounts/flagged/` | JWT | accounts over a bot-likelihood threshold |
| `GET /api/social/posts/` | JWT | posts with sentiment and toxicity |
| `POST /api/social/refresh-scores/` | JWT | rescore the whole graph, inline |
| `GET /api/social/campaigns/` | JWT | detected clusters, with the working behind each |
| `POST /api/social/campaigns/detect/` | JWT | run detection, inline |
| `GET /api/social/campaigns/latest_run/` | JWT | the last pass, its notes, and how many clusters it did not find again |
| `GET /api/social/campaign-runs/` | JWT | every detection pass, including the ones that found nothing |
| `GET /api/media/items/` `.../results/` | JWT | uploaded media and their tampering results |
| `POST /api/media/items/{id}/analyze/` | JWT | run forensics on one item, inline |
| `POST /api/media/items/analyze-pending/` | JWT | analyse everything not yet analysed |
| `GET /api/media/items/engine/` | JWT | which analyzer is configured and what it can do |
| `GET /api/reportgis/events/` | JWT | located events with their confidence and working |
| `GET /api/reportgis/events/geojson/` | JWT | the same events as GeoJSON |
| `GET /api/reportgis/events/summary/` | JWT | counts by confidence; counted in the DB |
| `POST /api/reportgis/events/extract/` | JWT | locate places in analysed articles, inline |
| `GET /api/reportgis/extractions/` | JWT | every extraction pass, including the empty ones |
| `POST /api/reportgis/reports/preview/` | JWT | what a report would contain, without writing one |
| `POST /api/reportgis/reports/generate/` | JWT | build a report; `GET .../{id}/download/` for the file |
| `GET /api/overview/` | JWT | the dashboard's per-module counts |
| `/admin/` | session | inspect all models |

## How the modules connect

1. Creating a `Source` fires a `post_save` signal that queues `score_source`
   on commit - scoring is never triggered by hand.
2. `fetch_source` checks that score *before* making a single request, then
   reads the source's feed and fetches what is new. `ingest_item` re-checks it
   for anything arriving by other routes. A source below
   `CONTENT_MIN_CREDIBILITY` (default 25) is rejected, and one that has never
   been scored is scored inline rather than waved through.
3. A stored `ContentItem` queues `analyze_content`, which writes entities,
   topics, and a summary. Re-running replaces prior output instead of
   duplicating it.
4. `analyze_content` then hands the analysed article to Module 5's
   `locate_content_item`, so the map fills as content arrives rather than
   waiting for someone to press a button. That call is guarded: a geocoder
   that breaks must not take a finished analysis down with it, and Module 2
   must keep working if Module 5 is removed entirely.
5. Corroborating an article reads Module 1's score for both the article's own
   source and every outlet that matched it, so a rating is weighted by who is
   doing the corroborating. The fact-check lookup runs alongside and
   independently - a fact-checker's ruling is worth citing whether or not the
   news search succeeded.
6. `refresh_graph_scores` rebuilds the interaction graph and rescores every
   account in one bulk update; `detect_campaigns` then reads the same graph
   for clusters and writes a `CampaignRun` whether or not it found any.
7. Every model write outside the excluded apps lands in `AuditLogEntry` with
   the acting user, IP, and a field-level diff.

**Analyst-triggered actions run inline, not queued.** The default broker is
`filesystem://` with no worker attached, so a queued action would return
202 Accepted to a console that then redraws the numbers it already had - the
button would look like it worked and would have done nothing. `.delay()` is
for the scheduled sweeps below, where nobody is waiting on the answer.

Periodic work is registered with Celery Beat (`DatabaseScheduler`, so schedules
are editable in the admin): hourly ingest, nightly rescore, nightly graph
refresh. Run it alongside the worker:

```bash
.venv/Scripts/celery.exe -A backend beat -l info
```
