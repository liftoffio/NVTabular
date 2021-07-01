from collections import defaultdict
from typing import Optional, Dict, Text

import torch

from nvtabular.column_group import ColumnGroup
import torchmetrics as tm


class Task(torch.nn.Module):
    def __init__(self,
                 loss,
                 metrics=None,
                 body: Optional[torch.nn.Module] = None,
                 pre: Optional[torch.nn.Module] = None):
        super().__init__()
        self.metrics = metrics
        self.loss = loss
        self.body = body
        self.pre = pre

    def forward(self, inputs, **kwargs):
        x = inputs
        if self.body:
            x = self.body(x)
        if self.pre:
            x = self.pre(x)

        return x

    def calculate_metrics(self, inputs, targets):
        outputs = {}
        for metric in self.metrics:
            outputs[metric.name] = metric(inputs, targets)

        return outputs

    def compute_loss(self, inputs, targets, training: bool = False) -> torch.Tensor:
        predictions = self(inputs)
        loss = self.loss(predictions, targets)

        return loss

    @classmethod
    def binary_classification(cls, metrics=None):
        metrics = metrics or [
            tm.Precision(),
            tm.Recall(),
            tm.Accuracy(),
            tm.AUC()
        ]

        return cls(
            loss=torch.nn.BCEWithLogitsLoss(),
            metrics=metrics,
        )

    @classmethod
    def regression(cls, metrics=None):
        metrics = metrics or [
            tm.regression.MeanSquaredError()
        ]

        return cls(
            loss=torch.nn.MSELoss(),
            metrics=metrics
        )


class Head(torch.nn.Module):
    def __init__(self, input_size=None):
        super().__init__()
        if isinstance(input_size, int):
            input_size = [input_size]
        self.input_size = input_size
        self.tasks = torch.nn.ModuleDict()
        self._task_weights = defaultdict(lambda: 1)

    def build(self, input_size, device=None):
        if device:
            self.to(device)
        self.input_size = input_size

    @classmethod
    def from_column_group(cls, column_group: ColumnGroup, add_logits=True, task_weights=None, input_size=None):
        if task_weights is None:
            task_weights = {}
        to_return = cls(input_size=input_size)

        for binary_target in column_group.binary_targets_columns:
            to_return = to_return.add_binary_classification_task(binary_target, add_logit_layer=add_logits,
                                                                 task_weight=task_weights.get(binary_target, 1))

        for regression_target in column_group.regression_targets_columns:
            to_return = to_return.add_regression_task(regression_target, add_logit_layer=add_logits,
                                                      task_weight=task_weights.get(regression_target, 1))

        # TODO: Add multi-class classification here. Figure out how to get number of classes

        return to_return

    def add_task(self, target_name, task: Task, pre: Optional[torch.nn.Module] = None, task_weight=1):
        self.tasks[target_name] = task
        if pre:
            self._tasks_prepares[target_name] = pre
        if task_weight:
            self._task_weights[target_name] = task_weight

        return self

    def add_binary_classification_task(self, target_name, add_logit_layer=True, task_weight=1):
        self.tasks[target_name] = Task.binary_classification()

        if add_logit_layer:
            self.tasks[target_name].pre = torch.nn.Linear(self.input_size[-1], 1)

        if task_weight:
            self._task_weights[target_name] = task_weight

        return self

    def add_regression_task(self, target_name, add_logit_layer=True, task_weight=1):
        self.tasks[target_name] = Task.regression()
        if add_logit_layer:
            self.tasks[target_name].pre = torch.nn.Linear(self.input_size[-1], 1)
        if task_weight:
            self._task_weights[target_name] = task_weight

        return self

    def pop_labels(self, inputs: Dict[Text, torch.Tensor]):
        outputs = {}
        for name in self.tasks.keys():
            outputs[name] = inputs.pop(name)

        return outputs

    def forward(self, logits: torch.Tensor, **kwargs):
        outputs = {}

        for name, task in self.tasks.items():
            outputs[name] = task(logits, **kwargs)

        return outputs

    def compute_loss(self, targets: Dict[Text, torch.Tensor], logits: Dict[Text, torch.Tensor],
                     **kwargs) -> torch.Tensor:
        losses = []

        for name, task in self.tasks.items():
            target, predictions = targets[name], logits[name] if isinstance(logits, dict) else logits
            losses.append(self.tasks[name].compute_loss(target, predictions, **kwargs) * self._task_weights[name])

        return torch.sum(*losses)