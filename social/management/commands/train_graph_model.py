"""Train the relational graph network on the accounts you have labelled.

This project ships no graph weights, and the reason is worth stating where
the training happens. A bot classifier has to be trained on accounts that
somebody looked at and judged. The public labelled Twitter graphs - TwiBot-20
and the Cresci collections - are released under access agreements with their
authors, and training on a re-upload to avoid signing one is routing around
the terms rather than meeting them. So the labels this command reads are the
ones an analyst set in the admin, on the accounts in this installation.

    manage.py train_graph_model
    manage.py train_graph_model --epochs 400 --hidden 64

That makes it a small-data trainer, and the command is built to say so. It
refuses to run below a floor, warns well above it, holds out a validation
split it never fits on, and prints what the model is worth on that split
before writing anything. A number from forty accounts is a number from forty
accounts; what this command must not do is let it read like more.

Two details that are not tuning choices:

*The graph is built over every account, the loss only over labelled ones.*
An unlabelled neighbour still carries messages, and dropping it would change
the shape the model is reading. This is the standard transductive setup.

*Standardisation uses the training rows' statistics, saved with the weights.*
Scoring later re-uses them. Re-deriving them from whoever is being scored
would hand the model a different column than it was fitted on, which is the
bug Module 1 shipped.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from social.features import FEATURE_NAMES, RELATIONS
from social.graphdata import load_graph
from social.models import SocialAccount

# Below this there is nothing to measure: a validation split of two accounts
# reports 50% or 100% and neither means anything.
MIN_LABELS = 12
MIN_PER_CLASS = 4
# Above the floor but below here, the command runs and keeps saying so.
COMFORTABLE = 120
FLOORS = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95)


class Command(BaseCommand):
    help = 'Train the R-GCN bot classifier on labelled accounts in this database.'

    def add_arguments(self, parser):
        parser.add_argument('--epochs', type=int, default=300)
        parser.add_argument('--hidden', type=int, default=32)
        parser.add_argument('--bases', type=int, default=8)
        parser.add_argument('--lr', type=float, default=0.01)
        parser.add_argument('--weight-decay', type=float, default=5e-4)
        parser.add_argument('--patience', type=int, default=60,
                            help='Stop after this many epochs with no '
                                 'validation improvement.')
        parser.add_argument('--val-fraction', type=float, default=0.3)
        parser.add_argument('--seed', type=int, default=20260101)
        parser.add_argument('--output', default=None)

    def say(self, line=''):
        """Print, unless the caller asked for quiet.

        Every line this command prints is part of its answer, so there is
        nothing to keep at verbosity 1 and drop at 0. The tests call it with
        verbosity 0 and want silence.
        """
        if self.verbosity:
            self.stdout.write(line)

    def handle(self, *args, **options):
        self.verbosity = options.get('verbosity', 1)
        from social.ml import (
            _rgcn_class,
            artifact_path,
            build_graph,
            ml_dependencies,
            standardise,
        )

        torch, _ = ml_dependencies()
        torch.manual_seed(options['seed'])

        nodes, edges = load_graph()
        if not nodes:
            raise CommandError('no accounts in the database.')

        labels = dict(
            SocialAccount.objects
            .filter(is_automated__isnull=False)
            .values_list('pk', 'is_automated')
        )
        self.check_labels(labels, len(nodes))

        graph = build_graph(nodes, edges)
        position = {account_id: i for i, account_id in enumerate(graph.account_ids)}

        self.say(
            f'{len(nodes)} accounts, {graph.edge_index.shape[1]} directed edges '
            f'(every relation in both directions), {len(labels)} labelled'
        )
        if graph.dropped_edges:
            self.say(
                f'{graph.dropped_edges} edge(s) dropped: an endpoint outside '
                'the account set, or an edge type the model has no relation for.'
            )

        train_ids, val_ids = self.split(labels, options)
        train_index = torch.tensor([position[i] for i in train_ids], dtype=torch.long)
        val_index = torch.tensor([position[i] for i in val_ids], dtype=torch.long)
        y = torch.zeros(len(graph.account_ids), dtype=torch.long)
        for account_id, automated in labels.items():
            y[position[account_id]] = 1 if automated else 0

        # Fitted on the training rows alone. Including the held-out rows
        # would leak their distribution into the scaling, and the split is
        # small enough for that to show.
        train_rows = graph.x[train_index]
        mean = train_rows.mean(dim=0)
        deviation = train_rows.std(dim=0)
        # A column that never varies in training carries no information;
        # dividing by its zero deviation would carry infinity instead.
        deviation = torch.where(deviation > 1e-6, deviation,
                                torch.ones_like(deviation))
        x = standardise(graph.x, mean, deviation)

        model = _rgcn_class()(len(FEATURE_NAMES), hidden=options['hidden'],
                              num_bases=options['bases'])
        weights = self.class_weights(torch, y[train_index])
        self.say(
            f'class weights {weights[0]:.2f} human / {weights[1]:.2f} automated, '
            'so the smaller class is not simply ignored'
        )

        best = self.fit(torch, model, x, graph, y, train_index, val_index,
                        weights, options)

        model.load_state_dict(best['state_dict'])
        model.eval()
        with torch.no_grad():
            probabilities = torch.softmax(
                model(x, graph.edge_index, graph.edge_type), dim=1
            )

        report = self.report(probabilities, y, val_index, best)
        out_path = Path(options['output'] or artifact_path())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            'state_dict': best['state_dict'],
            'feature_names': list(FEATURE_NAMES),
            'relations': list(RELATIONS),
            'hidden': options['hidden'],
            'num_bases': options['bases'],
            'mean': mean,
            'deviation': deviation,
            'metrics': report,
        }, out_path)
        self.say(self.style.SUCCESS(f'wrote {out_path}'))

        metrics_path = out_path.with_suffix('.metrics.json')
        metrics_path.write_text(json.dumps(report, indent=2) + '\n',
                                encoding='utf-8')
        self.say(self.style.SUCCESS(f'wrote {metrics_path}'))
        self.say('')
        self.say(
            'Set SOCIAL_GRAPH_SCORER=social.ml.RgcnGraphScorer to serve it, '
            'and SOCIAL_GRAPH_MIN_CONFIDENCE from the table above.'
        )

    # -- checks -----------------------------------------------------------

    def check_labels(self, labels, account_count):
        automated = sum(1 for value in labels.values() if value)
        human = len(labels) - automated
        if len(labels) < MIN_LABELS or min(automated, human) < MIN_PER_CLASS:
            raise CommandError(
                f'{len(labels)} labelled account(s): {automated} automated, '
                f'{human} not. Training needs at least {MIN_LABELS} with '
                f'{MIN_PER_CLASS} of each. Set is_automated in the admin on '
                'accounts you have actually reviewed; guessing at the labels '
                'produces a model that reproduces the guess.'
            )
        if len(labels) < COMFORTABLE:
            self.say(self.style.WARNING(
                f'{len(labels)} labelled accounts out of {account_count}. This '
                'will train, and the result is a model fitted on a handful of '
                'examples from one installation. Read the validation numbers '
                'below as a sanity check, not as a performance claim, and '
                'expect the confidence floor to silence most accounts.'
            ))

    # -- data -------------------------------------------------------------

    def split(self, labels, options):
        """Stratified holdout, seeded so two runs compare.

        Stratified because the classes are uneven and a random split of
        forty accounts can easily hand the held-out set one automated
        account, or none.
        """
        rng = random.Random(options['seed'])
        train_ids, val_ids = [], []
        for wanted in (True, False):
            members = sorted(i for i, v in labels.items() if bool(v) is wanted)
            rng.shuffle(members)
            held = max(1, round(len(members) * options['val_fraction']))
            # Never hold out so much that a class vanishes from training.
            held = min(held, len(members) - 1)
            val_ids.extend(members[:held])
            train_ids.extend(members[held:])
        self.say(
            f'{len(train_ids)} accounts to fit on, {len(val_ids)} held out'
        )
        return train_ids, val_ids

    @staticmethod
    def class_weights(torch, train_labels):
        counts = [int((train_labels == c).sum()) for c in (0, 1)]
        total = sum(counts)
        return torch.tensor(
            [total / (2 * count) if count else 0.0 for count in counts],
            dtype=torch.float,
        )

    # -- training ---------------------------------------------------------

    def fit(self, torch, model, x, graph, y, train_index, val_index, weights,
            options):
        """Fit, early-stopping on held-out loss, keeping the best state.

        Loss rather than accuracy: with a held-out split this small,
        accuracy moves in steps of one account and several epochs tie, so
        the "best" epoch by accuracy is whichever tied epoch came first.
        """
        optimiser = torch.optim.Adam(model.parameters(), lr=options['lr'],
                                     weight_decay=options['weight_decay'])
        loss_fn = torch.nn.CrossEntropyLoss(weight=weights)
        best = {
            'loss': float('inf'),
            'epoch': 0,
            'state_dict': {k: v.clone() for k, v in model.state_dict().items()},
        }
        since = 0

        for epoch in range(1, options['epochs'] + 1):
            model.train()
            optimiser.zero_grad()
            out = model(x, graph.edge_index, graph.edge_type)
            loss = loss_fn(out[train_index], y[train_index])
            loss.backward()
            optimiser.step()

            model.eval()
            with torch.no_grad():
                out = model(x, graph.edge_index, graph.edge_type)
                val_loss = float(loss_fn(out[val_index], y[val_index]))
                val_correct = int(
                    (out[val_index].argmax(dim=1) == y[val_index]).sum()
                )

            if val_loss < best['loss'] - 1e-5:
                best = {
                    'loss': val_loss,
                    'epoch': epoch,
                    'state_dict': {k: v.clone()
                                   for k, v in model.state_dict().items()},
                }
                since = 0
            else:
                since += 1

            if epoch == 1 or epoch % 25 == 0:
                self.say(
                    f'  epoch {epoch:>4}  train {loss.item():.4f}  '
                    f'val {val_loss:.4f}  val right {val_correct}/'
                    f'{len(val_index)}'
                )
            if since >= options['patience']:
                stopped = options['patience']
                self.say(
                    f'  stopped at epoch {epoch}: {stopped} epochs with no '
                    'improvement on the held-out split'
                )
                break

        self.say(
            f'best epoch {best["epoch"]}, held-out loss {best["loss"]:.4f}'
        )
        return best

    # -- reporting --------------------------------------------------------

    def report(self, probabilities, y, val_index, best):
        automated = [float(probabilities[i][1]) for i in val_index.tolist()]
        gold = [int(y[i]) for i in val_index.tolist()]
        predicted = [1 if p >= 0.5 else 0 for p in automated]
        accuracy = sum(g == p for g, p in zip(gold, predicted)) / len(gold)

        self.say('')
        self.say(
            f'held out: {len(gold)} accounts, {sum(gold)} automated, '
            f'{accuracy:.4f} accuracy at the 0.50 mark'
        )
        self.say('')
        self.say(f'{"floor":>6}  {"speaks on":>9}  {"right then":>10}')
        table = []
        for floor in FLOORS:
            kept = [(g, p) for g, p, a in zip(gold, predicted, automated)
                    if max(a, 1 - a) >= floor]
            coverage = len(kept) / len(gold)
            correct = (sum(g == p for g, p in kept) / len(kept)) if kept else 0.0
            table.append({'floor': floor, 'speaks_on': round(coverage, 4),
                          'right_when_it_speaks': round(correct, 4),
                          'accounts': len(kept)})
            self.say(
                f'{floor:>6.2f}  {coverage:>9.4f}  {correct:>10.4f}'
            )
        self.say('')
        self.say(
            'Read the right-hand column against the account count beside it '
            'in the JSON: at a high floor it is often a handful of accounts, '
            'and 1.0000 over six accounts is not a 100% accurate model.'
        )

        return {
            'held_out_accounts': len(gold),
            'held_out_automated': sum(gold),
            'accuracy_at_half': round(accuracy, 4),
            'best_epoch': best['epoch'],
            'best_val_loss': round(best['loss'], 4),
            'by_floor': table,
        }
