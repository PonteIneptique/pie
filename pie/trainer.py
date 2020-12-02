import os
import uuid
import logging
import time
import collections
import random
import tempfile


import tqdm

import torch
from torch import optim
from torch.optim.optimizer import Optimizer
from torch.nn.utils import clip_grad_norm_
import torch_optimizer as ext_optims
from typing import ClassVar

logging.basicConfig(format='%(asctime)s : %(message)s', level=logging.INFO)


def get_batch_task(tasks, **kwargs):
    tnames, target = [], None
    for task in tasks:
        tnames.append(task['name'])
        if task.get('target'):
            target = task['name']
    return sample_task(target, tnames, **kwargs)


def sample_task(target, tasks, factor=2):
    # sample target task factor times as many as any other task
    aux = (1 / (len(tasks) - 1)) / factor if len(tasks) > 1 else 0
    trg = 1 - (aux * (len(tasks) - 1))
    weights = [aux if task != target else trg for task in tasks]
    return random.choices(tasks, weights)[0]


def get_target_task(settings):
    for task in settings.tasks:
        if task.get('target'):
            return task['name']
    raise ValueError("No target task?")


class EarlyStopException(Exception):
    def __init__(self, task, loss, state_dict):
        self.task = task
        self.loss = loss
        self.best_state_dict = state_dict


class TaskScheduler(object):
    """
    Track scores
    """
    def __init__(self, settings):
        tasks = {}
        # preprocess tasks
        for task in settings.tasks:
            # add schedule and target
            tasks[task['name']] = task.get('schedule', {})
            tasks[task['name']]['target'] = task.get('target', False)
            # add task data for lm loss
            if settings.include_lm:
                tasks['lm_fwd'] = dict(settings.lm_schedule)
                tasks['lm_bwd'] = dict(settings.lm_schedule)

        for task, tdata in tasks.items():
            # set step counter
            tdata['steps'] = 0
            # set default task mode
            tdata['mode'] = tdata.get('mode', 'max')
            # set initial weight
            tdata['weight'] = tdata.get('weight', 1.0)
            # set initial best
            tdata['best'] = -float('inf') if tdata['mode'] == 'max' else float('inf')

        self.tasks = tasks

        # task schedule
        self.patience = settings.patience
        self.factor = settings.factor
        self.threshold = settings.threshold
        self.min_weight = settings.min_weight
        self.fid = os.path.join(tempfile.gettempdir(), str(uuid.uuid1()))

    def __repr__(self):
        # task scheduler
        output = (
            '<TaskScheduler patience="{}" factor="{}" ' +
            'threshold="{}" min_weight="{}">').format(
                self.patience, self.factor, self.threshold, self.min_weight)

        for task, values in self.tasks.items():
            output += '\n    <Task name="{}" '.format(task)
            output += ' '.join(
                '{}="{}"'.format(key, val) for key, val in values.items())
            output += '/>'
        output += '\n</TaskScheduler>'

        return output

    def is_best(self, task, value):
        threshold = self.tasks[task].get('threshold', self.threshold)
        mode = self.tasks[task]['mode']
        if mode.lower() == 'max':
            return value > (self.tasks[task]['best'] + threshold)
        elif mode.lower() == 'min':
            return value < (self.tasks[task]['best'] - threshold)
        else:
            raise ValueError("Wrong mode value [{}] for task: {}".format(mode, task))

    def step(self, scores, model):
        """
        Advance schedule step based on dev scores
        """
        for task, score in scores.items():
            if task not in self.tasks:
                # ignore
                continue

            is_target = self.tasks[task].get('target', False)

            # check if we improve
            if self.is_best(task, score):
                self.tasks[task]['best'] = score
                self.tasks[task]['steps'] = 0
                if is_target:
                    # serialize model params
                    torch.save(model.state_dict(), self.fid)
            else:
                self.tasks[task]['steps'] += 1

            # check if we need to stop globally or downweight a task loss
            patience = self.tasks[task].get('patience', self.patience)
            if self.tasks[task]['steps'] >= patience:
                # maybe stop entire training
                if is_target:
                    state_dict = torch.load(self.fid)
                    os.remove(self.fid)
                    raise EarlyStopException(task, self.tasks[task]['best'], state_dict)
                # update task weight
                else:
                    factor = self.tasks[task].get('factor', self.factor)
                    new_weight = self.tasks[task]['weight'] * factor
                    min_weight = self.tasks[task].get('min_weight', self.min_weight)
                    self.tasks[task]['weight'] = max(new_weight, min_weight)

    def get_weights(self):
        return {task: self.tasks[task]['weight'] for task in self.tasks}


class DelayerScheduler(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, delay: int, base_scheduler: torch.optim.lr_scheduler._LRScheduler):
        self.nb_steps = -1
        self.delay = delay
        self.base_scheduler = base_scheduler
        super(DelayerScheduler, self).__init__(optimizer)

    def step(self, *args, **kwargs):
        self.nb_steps += 1
        if self.steps > self.delay:
            self.base_scheduler.step(*args, **kwargs)

    @property
    def waiting(self):
        return self.steps <= self.delay

    @property
    def steps(self):
        return self.nb_steps


class Trainer(object):
    """
    Trainer

    Settings
    ========
    optim
    lr (lr_factor, lr_patience, min_lr)
    clip_norm
    weights
    report_freq
    checks_per_epoch
    """

    @staticmethod
    def get_optimizer(optimizer_name: str) -> ClassVar[Optimizer]:
        """ Allows for getting new optimizers from the torch-optimizer library without
        breaking previous behaviour

        :param optimizer_name: Optimizer Name, eg. Adam, SGD, Ranger
        :return: Optimizer class
        """
        if hasattr(optim, optimizer_name):
            return getattr(optim, optimizer_name)
        elif hasattr(ext_optims, optimizer_name):
            return getattr(ext_optims, optimizer_name)

    def print_lr_scheduler(self, lr_scheduler: optim.lr_scheduler._LRScheduler):
        """ Display information using print about a LRScheduler

        :param lr_scheduler:
        :return:
        """
        # If we use a Delayer, we print information about the delayer until it finishes waiting
        if isinstance(lr_scheduler, DelayerScheduler):
            if lr_scheduler.waiting:
                print('<LRScheduler type="{}" lr="{:g}" delay="{}" steps="{}"/>'.format(
                    type(lr_scheduler).__name__,
                    self.optimizer.param_groups[0]['lr'],
                    lr_scheduler.delay,
                    lr_scheduler.steps
                ))
            else:
                self.print_lr_scheduler(lr_scheduler.base_scheduler)
        # Continue to display former information for ReduceLROnPlateau
        elif isinstance(lr_scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            print('<LrScheduler type="{}" lr="{:g}" steps="{}" patience="{}" threshold="{}"/>'.format(
                type(lr_scheduler).__name__,
                self.optimizer.param_groups[0]['lr'],
                lr_scheduler.num_bad_epochs,
                lr_scheduler.patience,
                lr_scheduler.threshold
            ))
        # There are no specific information to display for some schedulers if not all
        else:
            print('<LrScheduler type="{}" lr="{:g}"/>'.format(
                type(lr_scheduler).__name__,
                self.optimizer.param_groups[0]['lr']
            ))

    def get_scheduler(self, settings) -> optim.lr_scheduler._LRScheduler:
        """ Initialize a LRScheduler based on settings

        :param settings: Settings fed through JSON
        :return: The LRScheduler required by the settings, disregarding delay
        """
        if not self.optimizer:
            raise Exception("Scheduler needs to be set after optimizer")
        if settings.lr_scheduler == "ReduceLROnPlateau":
            return optim.lr_scheduler.ReduceLROnPlateau(
                self.optimizer, mode='max', factor=settings.lr_factor,
                patience=settings.lr_patience, min_lr=settings.min_lr
            )
        elif settings.lr_scheduler == "CosineAnnealingLR":
            return optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer, T_max=settings.lr_T_max, eta_min=settings.min_lr
            )
        elif settings.lr_scheduler == "CosineAnnealingWarmRestarts":
            return optim.lr_scheduler.CosineAnnealingWarmRestarts(
                self.optimizer, T_0=settings.lr_T_0, eta_min=settings.min_lr
            )
        else:
            raise ValueError(f"Unknown scheduler {settings.lr_scheduler}")

    def step_lr_scheduler(self, loss):
        """ Apply a step to the LRScheduler.

        Some scheduler use loss as the information for steps, some use the epoch_id.

        :param loss: Loss computed from the TaskScheduler
        """
        use_loss = isinstance(self.lr_scheduler, optim.lr_scheduler.ReduceLROnPlateau)
        if isinstance(self.lr_scheduler, DelayerScheduler):
            use_loss = isinstance(self.lr_scheduler.base_scheduler, optim.lr_scheduler.ReduceLROnPlateau)

        if use_loss:
            self.lr_scheduler.step(metrics=loss)
        else:
            self.lr_scheduler.step(epoch=None)

    def __init__(self, settings, model, dataset, num_instances):
        self.target_task = get_target_task(settings)
        self.verbose = settings.verbose
        self.dataset = dataset
        self.model = model
        self.optimizer = self.get_optimizer(settings.optimizer)(
            model.parameters(), lr=settings.lr)
        self.clip_norm = settings.clip_norm

        self.report_freq = settings.report_freq
        self.num_batches = num_instances // dataset.batch_size
        if settings.checks_per_epoch == 1:
            self.check_freq = self.num_batches - 1  # check after last batch
        elif settings.checks_per_epoch > self.num_batches:
            self.check_freq = 1  # check after each batch
        elif settings.checks_per_epoch > 1:
            self.check_freq = self.num_batches // settings.checks_per_epoch  # check just
        else:
            self.check_freq = 0  # no checks

        self.task_scheduler = TaskScheduler(settings)

        lr_scheduler: optim.lr_scheduler._LRScheduler = self.get_scheduler(
            settings
        )
        if settings.lr_delayed > 0:
            self.lr_scheduler = DelayerScheduler(
                optimizer=self.optimizer,
                delay=settings.lr_delayed,
                base_scheduler=lr_scheduler
            )
        else:
            self.lr_scheduler = lr_scheduler

        if settings.verbose:
            print()
            print("Evaluation check every {}/{} batches".format(
                self.check_freq, self.num_batches))
            print()
            print("::: Task schedules :::")
            print()
            print(self.task_scheduler)
            print()
            print("::: LR schedule :::")
            print()
            self.print_lr_scheduler(self.lr_scheduler)
            print()

    def weight_loss(self, loss):
        """
        Apply weights to losses and return a single loss number
        """
        weights = self.task_scheduler.get_weights()

        return sum(weights.get(k, 1) * loss[k] for k in loss)

    def evaluate(self, dataset):
        """
        Evaluate objective on held-out data
        """
        total_losses, total_batches = collections.defaultdict(float), 0

        # get all tasks
        tasks = list(self.model.tasks)

        for batch in tqdm.tqdm(dataset.batch_generator()):
            total_batches += 1
            for k, v in self.model.loss(batch, *tasks).items():
                total_losses[k] += v.item()

        for k, v in total_losses.items():
            total_losses[k] = v / total_batches

        return dict(total_losses)

    def run_check(self, devset):
        """
        Monitor dev loss and eventually early-stop training
        """
        print()
        print("Evaluating model on dev set...")
        print()

        self.model.eval()

        stored_scores = {}

        with torch.no_grad():
            dev_loss = self.evaluate(devset)
            print()
            print("::: Dev losses :::")
            print()
            print('\n'.join('{}: {:.4f}'.format(k, v) for k, v in dev_loss.items()))
            print()
            summary = self.model.evaluate(devset, self.dataset)
            for task_name, scorer in summary.items():
                stored_scores[task_name] = scorer.get_scores()
                scorer.print_summary(scores=stored_scores[task_name])

        self.model.train()
        dev_scores = {}
        for task, scored in stored_scores.items():
            dev_scores[task] = scored['all']['accuracy']
        # add lm scores
        if 'lm_fwd' in dev_loss or 'lm_bwd' in dev_loss:
            dev_scores['lm_fwd'] = dev_loss['lm_fwd']
            dev_scores['lm_bwd'] = dev_loss['lm_bwd']

        self.task_scheduler.step(dev_scores, self.model)
        self.step_lr_scheduler(loss=dev_scores[self.target_task])

        if self.verbose:
            print(self.task_scheduler)
            print()
            self.print_lr_scheduler(self.lr_scheduler)
            print()

        return dev_scores

    def train_epoch(self, devset, epoch):
        rep_loss = collections.defaultdict(float)
        rep_batches = collections.defaultdict(int)
        rep_items, rep_start = 0, time.time()
        scores = None

        for b, batch in enumerate(self.dataset.batch_generator()):
            # get loss
            loss = self.model.loss(batch, get_batch_task(self.model.tasks.values()))

            if not loss:
                raise ValueError("Got empty loss, no tasks defined?")

            # optimize
            self.optimizer.zero_grad()
            self.weight_loss(loss).backward()
            if self.clip_norm > 0:
                clip_grad_norm_(self.model.parameters(), self.clip_norm)
            self.optimizer.step()

            # accumulate
            rep_items += type(self.dataset).get_nelement(batch)
            for k, v in loss.items():
                rep_batches[k] += 1
                rep_loss[k] += v.item()

            # report
            if b > 0 and b % self.report_freq == 0:
                rep = ""
                for t in sorted(rep_loss):
                    rep += '{}:{:.4f}  '.format(t, rep_loss[t] / rep_batches[t])
                logging.info("Batch [{}/{}] || {} || {:.0f} w/s".format(
                    b, self.num_batches, rep, rep_items / (time.time() - rep_start)))
                rep_loss = collections.defaultdict(float)
                rep_batches = collections.defaultdict(int)
                rep_items, rep_start = 0, time.time()

            if self.check_freq > 0 and b > 0 and b % self.check_freq == 0:
                if devset is not None:
                    rep_start = time.time()
                    scores = self.run_check(devset)
                    logging.info("Evaluation time: {:.0f} sec".format(
                        time.time() - rep_start))
                    rep_start = time.time()

        return scores

    def train_epochs(self, epochs, devset=None):
        """
        Train the model for a number of epochs
        """
        start = time.time()
        scores = None

        try:
            for epoch in range(1, epochs + 1):
                # train epoch
                epoch_start = time.time()
                logging.info("Starting epoch [{}]".format(epoch))
                self.train_epoch(devset, epoch)
                epoch_total = time.time() - epoch_start
                logging.info("Finished epoch [{}] in [{:.0f}] secs".format(
                    epoch, epoch_total))

        except EarlyStopException as e:
            logging.info("Early stopping training: "
                         "task [{}] with best score {:.4f}".format(e.task, e.loss))

            self.model.load_state_dict(e.best_state_dict)
            scores = {e.task: e.loss}

        logging.info("Finished training in [{:.0f}] secs".format(time.time() - start))

        # will be None if no dev test was provided or the model failed to converge
        return scores
