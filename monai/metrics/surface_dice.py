# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Any

import numpy as np
import torch

from monai.metrics.utils import (
    do_metric_reduction,
    get_mask_edges,
    get_surface_distance,
    ignore_background,
    prepare_spacing,
)
from monai.utils import MetricReduction, convert_data_type

from .metric import CumulativeIterationMetric


class SurfaceDiceMetric(CumulativeIterationMetric):
    """
    Computes the Normalized Surface Distance (NSD) for each batch sample and class of
    predicted segmentations `y_pred` and corresponding reference segmentations `y` according to equation :eq:`nsd`.
    This implementation supports 2D images. For 3D images, please refer to DeepMind's implementation
    https://github.com/deepmind/surface-distance.

    The class- and batch sample-wise NSD values can be aggregated with the function `aggregate`.

    Example of the typical execution steps of this metric class follows :py:class:`monai.metrics.metric.Cumulative`.

    Args:
        class_thresholds: List of class-specific thresholds.
            The thresholds relate to the acceptable amount of deviation in the segmentation boundary in pixels.
            Each threshold needs to be a finite, non-negative number.
        include_background: Whether to skip NSD computation on the first channel of the predicted output.
            Defaults to ``False``.
        distance_metric: The metric used to compute surface distances.
            One of [``"euclidean"``, ``"chessboard"``, ``"taxicab"``].
            Defaults to ``"euclidean"``.
        reduction: define mode of reduction to the metrics, will only apply reduction on `not-nan` values,
            available reduction modes: {``"none"``, ``"mean"``, ``"sum"``, ``"mean_batch"``, ``"sum_batch"``,
            ``"mean_channel"``, ``"sum_channel"``}, default to ``"mean"``. if "none", will not do reduction.
        get_not_nans: whether to return the `not_nans` count.
            Defaults to ``False``.
            `not_nans` is the number of batch samples for which not all class-specific NSD values were nan values.
            If set to ``True``, the function `aggregate` will return both the aggregated NSD and the `not_nans` count.
            If set to ``False``, `aggregate` will only return the aggregated NSD.
    """

    def __init__(
        self,
        class_thresholds: list[float],
        include_background: bool = False,
        distance_metric: str = "euclidean",
        reduction: MetricReduction | str = MetricReduction.MEAN,
        get_not_nans: bool = False,
    ) -> None:
        super().__init__()
        self.class_thresholds = class_thresholds
        self.include_background = include_background
        self.distance_metric = distance_metric
        self.reduction = reduction
        self.get_not_nans = get_not_nans

    def _compute_tensor(self, y_pred: torch.Tensor, y: torch.Tensor, **kwargs: Any) -> torch.Tensor:  # type: ignore[override]
        r"""
        Args:
            y_pred: Predicted segmentation, typically segmentation model output.
                It must be a one-hot encoded, batch-first tensor [B,C,H,W].
            y: Reference segmentation.
                It must be a one-hot encoded, batch-first tensor [B,C,H,W].
            kwargs: additional parameters, e.g. ``spacing`` should be passed to correctly compute the metric.
                ``spacing``: spacing of pixel (or voxel). This parameter is relevant only
                if ``distance_metric`` is set to ``"euclidean"``.
                If a single number, isotropic spacing with that value is used for all images in the batch. If a sequence of numbers,
                the length of the sequence must be equal to the image dimensions.
                This spacing will be used for all images in the batch.
                If a sequence of sequences, the length of the outer sequence must be equal to the batch size.
                If inner sequence has length 1, isotropic spacing with that value is used for all images in the batch,
                else the inner sequence length must be equal to the image dimensions. If ``None``, spacing of unity is used
                for all images in batch. Defaults to ``None``.

        Returns:
            Pytorch Tensor of shape [B,C], containing the NSD values :math:`\operatorname {NSD}_{b,c}` for each batch
            index :math:`b` and class :math:`c`.
        """
        return compute_surface_dice(
            y_pred=y_pred,
            y=y,
            class_thresholds=self.class_thresholds,
            include_background=self.include_background,
            distance_metric=self.distance_metric,
            spacing=kwargs.get("spacing"),
        )

    def aggregate(
        self, reduction: MetricReduction | str | None = None
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
        r"""
        Aggregates the output of `_compute_tensor`.

        Args:
            reduction: define mode of reduction to the metrics, will only apply reduction on `not-nan` values,
                available reduction modes: {``"none"``, ``"mean"``, ``"sum"``, ``"mean_batch"``, ``"sum_batch"``,
                ``"mean_channel"``, ``"sum_channel"``}, default to `self.reduction`. if "none", will not do reduction.

        Returns:
            If `get_not_nans` is set to ``True``, this function returns the aggregated NSD and the `not_nans` count.
            If `get_not_nans` is set to ``False``, this function returns only the aggregated NSD.
        """
        data = self.get_buffer()
        if not isinstance(data, torch.Tensor):
            raise ValueError("the data to aggregate must be PyTorch Tensor.")

        # do metric reduction
        f, not_nans = do_metric_reduction(data, reduction or self.reduction)
        return (f, not_nans) if self.get_not_nans else f


def compute_surface_dice(
    y_pred: torch.Tensor,
    y: torch.Tensor,
    class_thresholds: list[float],
    include_background: bool = False,
    distance_metric: str = "euclidean",
    spacing: int | float | np.ndarray | Sequence[int | float | np.ndarray | Sequence[int | float]] | None = None,
) -> torch.Tensor:
    r"""
    This function computes the (Normalized) Surface Dice (NSD) between the two tensors `y_pred` (referred to as
    :math:`\hat{Y}`) and `y` (referred to as :math:`Y`). This metric determines which fraction of a segmentation
    boundary is correctly predicted. A boundary element is considered correctly predicted if the closest distance to the
    reference boundary is smaller than or equal to the specified threshold related to the acceptable amount of deviation in
    pixels. The NSD is bounded between 0 and 1.

    This implementation supports multi-class tasks with an individual threshold :math:`\tau_c` for each class :math:`c`.
    The class-specific NSD for batch index :math:`b`, :math:`\operatorname {NSD}_{b,c}`, is computed using the function:

    .. math::
        \operatorname {NSD}_{b,c} \left(Y_{b,c}, \hat{Y}_{b,c}\right) = \frac{\left|\mathcal{D}_{Y_{b,c}}^{'}\right| +
        \left| \mathcal{D}_{\hat{Y}_{b,c}}^{'} \right|}{\left|\mathcal{D}_{Y_{b,c}}\right| +
        \left|\mathcal{D}_{\hat{Y}_{b,c}}\right|}
        :label: nsd

    with :math:`\mathcal{D}_{Y_{b,c}}` and :math:`\mathcal{D}_{\hat{Y}_{b,c}}` being two sets of nearest-neighbor
    distances. :math:`\mathcal{D}_{Y_{b,c}}` is computed from the predicted segmentation boundary towards the reference segmentation
    boundary and vice-versa for :math:`\mathcal{D}_{\hat{Y}_{b,c}}`. :math:`\mathcal{D}_{Y_{b,c}}^{'}` and
    :math:`\mathcal{D}_{\hat{Y}_{b,c}}^{'}` refer to the subsets of distances that are smaller or equal to the
    acceptable distance :math:`\tau_c`:

    .. math::
        \mathcal{D}_{Y_{b,c}}^{'} = \{ d \in \mathcal{D}_{Y_{b,c}} \, | \, d \leq \tau_c \}.


    In the case of a class neither being present in the predicted segmentation, nor in the reference segmentation, a nan value
    will be returned for this class. In the case of a class being present in only one of predicted segmentation or
    reference segmentation, the class NSD will be 0.

    This implementation is based on https://arxiv.org/abs/2111.05408 and supports 2D images.
    Be aware that the computation of boundaries is different from DeepMind's implementation
    https://github.com/deepmind/surface-distance. In this implementation, the length of a segmentation boundary is
    interpreted as the number of its edge pixels. In DeepMind's implementation, the length of a segmentation boundary
    depends on the local neighborhood (cf. https://arxiv.org/abs/1809.04430).

    Args:
        y_pred: Predicted segmentation, typically segmentation model output.
            It must be a one-hot encoded, batch-first tensor [B,C,H,W].
        y: Reference segmentation.
            It must be a one-hot encoded, batch-first tensor [B,C,H,W].
        class_thresholds: List of class-specific thresholds.
            The thresholds relate to the acceptable amount of deviation in the segmentation boundary in pixels.
            Each threshold needs to be a finite, non-negative number.
        include_background: Whether to skip the surface dice computation on the first channel of
            the predicted output. Defaults to ``False``.
        distance_metric: The metric used to compute surface distances.
            One of [``"euclidean"``, ``"chessboard"``, ``"taxicab"``].
            Defaults to ``"euclidean"``.
        spacing: spacing of pixel (or voxel). This parameter is relevant only if ``distance_metric`` is set to ``"euclidean"``.
            If a single number, isotropic spacing with that value is used for all images in the batch. If a sequence of numbers,
            the length of the sequence must be equal to the image dimensions. This spacing will be used for all images in the batch.
            If a sequence of sequences, the length of the outer sequence must be equal to the batch size.
            If inner sequence has length 1, isotropic spacing with that value is used for all images in the batch,
            else the inner sequence length must be equal to the image dimensions. If ``None``, spacing of unity is used
            for all images in batch. Defaults to ``None``.

    Raises:
        ValueError: If `y_pred` and/or `y` are not PyTorch tensors.
        ValueError: If `y_pred` and/or `y` do not have four dimensions.
        ValueError: If `y_pred` and/or `y` have different shapes.
        ValueError: If `y_pred` and/or `y` are not one-hot encoded
        ValueError: If the number of channels of `y_pred` and/or `y` is different from the number of class thresholds.
        ValueError: If any class threshold is not finite.
        ValueError: If any class threshold is negative.

    Returns:
        Pytorch Tensor of shape [B,C], containing the NSD values :math:`\operatorname {NSD}_{b,c}` for each batch index
        :math:`b` and class :math:`c`.
    """

    if not include_background:
        y_pred, y = ignore_background(y_pred=y_pred, y=y)

    if not isinstance(y_pred, torch.Tensor) or not isinstance(y, torch.Tensor):
        raise ValueError("y_pred and y must be PyTorch Tensor.")

    if y_pred.ndimension() != 4 or y.ndimension() != 4:
        raise ValueError("y_pred and y should have four dimensions: [B,C,H,W].")

    if y_pred.shape != y.shape:
        raise ValueError(
            f"y_pred and y should have same shape, but instead, shapes are {y_pred.shape} (y_pred) and {y.shape} (y)."
        )

    if not torch.all(y_pred.byte() == y_pred) or not torch.all(y.byte() == y):
        raise ValueError("y_pred and y should be binarized tensors (e.g. torch.int64).")
    if torch.any(y_pred > 1) or torch.any(y > 1):
        raise ValueError("y_pred and y should be one-hot encoded.")

    y = y.float()
    y_pred = y_pred.float()

    batch_size, n_class = y_pred.shape[:2]

    if n_class != len(class_thresholds):
        raise ValueError(
            f"number of classes ({n_class}) does not match number of class thresholds ({len(class_thresholds)})."
        )

    if any(~np.isfinite(class_thresholds)):
        raise ValueError("All class thresholds need to be finite.")

    if any(np.array(class_thresholds) < 0):
        raise ValueError("All class thresholds need to be >= 0.")

    nsd = np.empty((batch_size, n_class))

    img_dim = y_pred.ndim - 2
    spacing_list = prepare_spacing(spacing=spacing, batch_size=batch_size, img_dim=img_dim)

    for b, c in np.ndindex(batch_size, n_class):
        (edges_pred, edges_gt) = get_mask_edges(y_pred[b, c], y[b, c], crop=False)
        if not np.any(edges_gt):
            warnings.warn(f"the ground truth of class {c} is all 0, this may result in nan/inf distance.")
        if not np.any(edges_pred):
            warnings.warn(f"the prediction of class {c} is all 0, this may result in nan/inf distance.")

        distances_pred_gt = get_surface_distance(
            edges_pred, edges_gt, distance_metric=distance_metric, spacing=spacing_list[b]
        )
        distances_gt_pred = get_surface_distance(
            edges_gt, edges_pred, distance_metric=distance_metric, spacing=spacing_list[b]
        )

        boundary_complete = len(distances_pred_gt) + len(distances_gt_pred)
        boundary_correct = np.sum(distances_pred_gt <= class_thresholds[c]) + np.sum(
            distances_gt_pred <= class_thresholds[c]
        )

        if boundary_complete == 0:
            # the class is neither present in the prediction, nor in the reference segmentation
            nsd[b, c] = np.nan
        else:
            nsd[b, c] = boundary_correct / boundary_complete

    return convert_data_type(nsd, output_type=torch.Tensor, device=y_pred.device, dtype=torch.float)[0]
