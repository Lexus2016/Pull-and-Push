"""Metric adapters: turn an artifact run into an objective metrics vector."""

from .base import MetricAdapter, MetricResult, get_metric_adapter

__all__ = ["MetricAdapter", "MetricResult", "get_metric_adapter"]
