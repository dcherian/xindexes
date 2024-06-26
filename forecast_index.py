from __future__ import annotations

import copy
import datetime
import enum
import itertools
from dataclasses import dataclass
from typing import Hashable

import numpy as np
import pandas as pd
import xarray as xr
from xarray.core.indexes import Index, PandasIndex
from xarray.core.indexing import IndexSelResult, merge_sel_results

Timestamp = str | datetime.datetime | pd.Timestamp | np.datetime64
Timedelta = str | datetime.timedelta | np.timedelta64  # TODO: pd.DateOffset also?


####################
####  Indexer types
# reference: https://www.unidata.ucar.edu/presentations/caron/FmrcPoster.pdf


class Model(enum.StrEnum):
    HRRR = enum.auto()


@dataclass(init=False)
class ModelRun:
    """
    The complete results for a single run is a model run dataset.

    Parameters
    ----------
    time: Timestamp-like
        Initialization time for model run
    """

    time: pd.Timestamp

    def __init__(self, time: Timestamp):
        self.time = pd.Timestamp(time)

    def get_indexer(
        self, model: Model | None, time_index: pd.DatetimeIndex, period_index: pd.TimedeltaIndex
    ) -> tuple[int, slice]:
        time_idxr = time_index.get_loc(self.time)
        period_idxr = slice(None)

        if model is Model.HRRR and self.time.hour % 6 != 0:
            period_idxr = slice(19)

        return time_idxr, period_idxr


@dataclass(init=False)
class ConstantOffset:
    """
    A constant offset dataset is created from all the data that have the same offset time.
    Offset here refers to a variable usually named `step` or `lead` or with CF standard name
    `forecast_period`.
    """

    step: pd.Timedelta

    def __init__(self, step: Timedelta):
        self.step = pd.Timedelta(step)

    def get_indexer(
        self, model: Model | None, time_index: pd.DatetimeIndex, period_index: pd.TimedeltaIndex
    ) -> tuple[slice | np.ndarray, int]:
        time_idxr = slice(None)
        (period_idxr,) = period_index.get_indexer([self.step])

        if model is Model.HRRR and self.step.total_seconds() / 3600 > 18:
            model_mask = np.ones(time_index.shape, dtype=bool)
            model_mask[time_index.hour % 6 != 0] = False
            time_idxr = np.arange(time_index.size)[model_mask]

        return time_idxr, period_idxr


@dataclass(init=False)
class ConstantForecast:
    """
    A constant forecast dataset is created from all the data that have the same forecast/valid time.

    Parameters
    ----------
    time: Timestamp-like

    """

    time: pd.Timestamp

    def __init__(self, time: Timestamp):
        self.time = pd.Timestamp(time)

    def get_indexer(
        self, model: Model | None, time_index: pd.DatetimeIndex, period_index: pd.TimedeltaIndex
    ) -> tuple[np.ndarray, np.ndarray]:
        target = self.time
        max_timedelta = period_index[-1]

        # earliest timestep we can start at
        earliest = target - max_timedelta
        left = time_index.get_slice_bound(earliest, side="left")

        # latest we can get
        right = time_index.get_slice_bound(target, side="right")

        needed_times = time_index[slice(left, right)]
        needed_steps = target - needed_times  # type: ignore
        # print(needed_times, needed_steps)

        needed_time_idxs = np.arange(left, right)
        needed_step_idxs = period_index.get_indexer(needed_steps)
        model_mask = np.ones(needed_time_idxs.shape, dtype=bool)

        # TODO: refactor this out
        if model is Model.HRRR:
            model_mask[(needed_times.hour != 6) & (needed_steps > pd.to_timedelta("18h"))] = False

        # It's possible we don't have the right step.
        # If pandas doesn't find an exact match it returns -1.
        mask = needed_step_idxs != -1

        needed_step_idxs = needed_step_idxs[model_mask & mask]
        needed_time_idxs = needed_time_idxs[model_mask & mask]

        assert needed_step_idxs.size == needed_time_idxs.size

        return needed_time_idxs, needed_step_idxs


@dataclass
class BestEstimate:
    """
    For each forecast time in the collection, the best estimate for that hour is used to create a
    best estimate dataset, which covers the entire time range of the collection.
    """

    # To restrict the start of the slice,
    # just use the standard `.sel(time=slice(since, None))`
    # TODO: `since` could be a timedelta relative to `asof`.
    # since: pd.Timestamp | None = None
    asof: pd.Timestamp | None = None

    def __post_init__(self):
        if self.asof is not None and self.asof < self.since:
            raise ValueError(
                "Can't request best estimate since {since=!r} "
                "which is earlier than requested {asof=!r}"
            )

    def get_indexer(
        self, model: Model | None, time_index: pd.DatetimeIndex, period_index: pd.TimedeltaIndex
    ) -> tuple[np.ndarray, np.ndarray]:
        if period_index[0] != pd.Timedelta(0):
            raise ValueError(
                "Can't make a best estimate dataset if forecast_period doesn't start at 0."
            )

        # TODO: consolidate the get_indexer lookup
        # if self.since is None:
        first_index = 0
        # else:
        # (first_index,) = time_index.get_indexer([self.since])

        last_index = time_index.size - 1 if self.asof is None else time_index.get_loc(self.asof)

        # TODO: refactor to a Model dataclass that does this filtering appropriately.
        if model is Model.HRRR and time_index[last_index].hour % 6 != 0:
            nsteps = 19
        else:
            nsteps = period_index.size

        needed_time_idxrs = np.concatenate(
            [
                np.arange(first_index, last_index, dtype=int),
                np.repeat(last_index, nsteps),
            ]
        )
        needed_step_idxrs = np.concatenate(
            [np.zeros((last_index - first_index,), dtype=int), np.arange(nsteps)]
        )

        return needed_time_idxrs, needed_step_idxrs


@dataclass
class Indexes:
    reference_time: PandasIndex
    period: PandasIndex

    def get_names(self) -> [Hashable, Hashable]:
        # TODO: is this dependable?
        return {"reference_time": self.reference_time.index.name, "period": self.period.index.name}


class ForecastIndex(Index):
    """
    An Xarray custom Index that allows indexing a forecast data-cube with
    `forecast_reference_time` (commonly `init`) and `forecast_period`
    (commonly `step` or `lead`) dimensions as _Forecast Model Run Collections_.


    Examples
    --------
    To do FMRC-style indexing, you'll need to first add a "dummy" scalar variable,
    say `"forecast"`.

    >>> from forecast_index import (
    ...     BestEstimate,
    ...     ConstantForecast,
    ...     ConstantOffset,
    ...     ForecastIndex,
    ...     ModelRun,
    ...     Model,
    ... )
    >>> ds.coords["forecast"] = 0

    Create the new index where `time` is the `forecast_reference_time` dimension,
    and `step` is the `forecast_period` dimension.
    >>> newds = ds.drop_indexes(["time", "step"]).set_xindex(
        ["time", "step", "forecast"], ForecastIndex, model=Model.HRRR
        )
    >>> newds

    Use `forecast` to indicate FMRC-style indexing

    >>> newds.sel(forecast=BestEstimate())

    >>> newds.sel(forecast=ConstantForecast("2024-05-20"))

    >>> newds.sel(forecast=ConstantOffset("32h"))

    >>> newds.sel(forecast=ModelRun("2024-05-20 13:00"))
    """

    def __init__(self, variables: Indexes, dummy_name: str, model: Model | None = None):
        self._indexes = variables

        assert isinstance(dummy_name, str)
        self.dummy_name = dummy_name
        self.model = model

        # We use "reference_time", "period" as internal references.
        self.names = variables.get_names()

    @classmethod
    def from_variables(cls, variables, options):
        """
        Must be created from three variables:
        1. A dummy scalar `forecast` variable.
        2. A variable with the CF attribute`standard_name: "forecast_reference_time"`.
        3. A variable with the CF attribute`standard_name: "forecast_period"`.
        """
        assert len(variables) == 3

        indexes = {}
        for k in ["forecast_reference_time", "forecast_period"]:
            for name, var in variables.items():
                std_name = var.attrs.get("standard_name", None)
                if k == std_name:
                    indexes[k.removeprefix("forecast_")] = PandasIndex.from_variables(
                        {name: var}, options={}
                    )
                elif var.ndim == 0:
                    dummy_name = name

        return cls(Indexes(**indexes), dummy_name=dummy_name, **options)

    def sel(self, labels, **kwargs):
        """
        Allows three kinds of indexing
        1. Along the dummy "forecast" variable: enable specialized methods using
           ConstantOffset, ModelRun, ConstantForecast, BestEstimate
        2. Along the `forecast_reference_time` dimension, identical to ModelRun
        3. Along the `forecast_period` dimension, indentical to ConstantOffset

        You cannot mix (1) with (2) or (3), but (2) and (3) can be combined in a single statement.
        """
        if self.dummy_name in labels and len(labels) != 1:
            raise ValueError(
                f"Indexing along {self.dummy_name!r} cannot be combined with "
                f"indexing along {tuple(self.names)!r}"
            )

        time_name, period_name = self.names["reference_time"], self.names["period"]

        # This allows normal `.sel` along `time_name` and `period_name` to work
        if time_name in labels or period_name in labels:
            if self.dummy_name in labels:
                raise ValueError(
                    f"Selecting along {time_name!r} or {period_name!r} cannot "
                    f"be combined with FMRC-style indexing along {self.dummy_name!r}."
                )
            time_index, period_index = self._indexes.reference_time, self._indexes.period
            new_indexes = copy.deepcopy(self._indexes)
            results = []
            if time_name in labels:
                result = time_index.sel({time_name: labels[time_name]}, **kwargs)
                results.append(result)
                idxr = result.dim_indexers[time_name]
                new_indexes.reference_time = new_indexes.reference_time[idxr]
            if period_name in labels:
                result = period_index.sel({period_name: labels[period_name]}, **kwargs)
                results.append(result)
                idxr = result.dim_indexers[period_name]
                new_indexes.period = new_indexes.period[idxr]
            new_index = type(self)(new_indexes, dummy_name=self.dummy_name, model=self.model)
            results.append(
                IndexSelResult(
                    {}, indexes={k: new_index for k in [self.dummy_name, time_name, period_name]}
                )
            )
            return merge_sel_results(results)

        assert len(labels) == 1
        assert next(iter(labels.keys())) == self.dummy_name

        label: ConstantOffset | ModelRun | ConstantForecast | BestEstimate
        label = next(iter(labels.values()))

        time_index: pd.DatetimeIndex = self._indexes.reference_time.index  # type: ignore[assignment]
        period_index: pd.TimedeltaIndex = self._indexes.period.index  # type: ignore[assignment]

        time_idxr, period_idxr = label.get_indexer(self.model, time_index, period_index)

        indexer, indexes, variables = {}, {}, {}
        match label:
            case ConstantOffset():
                indexer[time_name] = time_idxr
                indexer[period_name] = period_idxr
                indexes[time_name] = self._indexes.reference_time[time_idxr]
                valid_time_dim = time_name

            case ModelRun():
                indexer[time_name] = time_idxr
                indexer[period_name] = period_idxr
                indexes[period_name] = self._indexes.period[period_idxr]
                valid_time_dim = period_name

            case ConstantForecast():
                indexer = {
                    time_name: xr.Variable(time_name, time_idxr),
                    period_name: xr.Variable(time_name, period_idxr),
                }
                indexes[time_name] = self._indexes.reference_time[time_idxr]
                # TODO: this triggers a bug.
                # variables["valid_time"] = xr.Variable((), label.time, {"standard_name": "time"})

            case BestEstimate():
                indexer = {
                    time_name: xr.Variable("valid_time", time_idxr),
                    period_name: xr.Variable("valid_time", period_idxr),
                }
                valid_time_dim = "valid_time"

            case _:
                raise ValueError(f"Invalid indexer type {type(label)} for label: {label}")

        if not isinstance(label, ConstantForecast):
            valid_time = time_index[time_idxr] + period_index[period_idxr]
            variables["valid_time"] = xr.Variable(
                valid_time_dim, data=valid_time, attrs={"standard_name": "time"}
            )
            indexes["valid_time"] = PandasIndex(valid_time, dim=valid_time_dim)

        return IndexSelResult(
            dim_indexers=indexer, indexes=indexes, variables=variables, drop_coords=["forecast"]
        )

    def __repr__(self):
        string = (
            "<ForecastIndex along ["
            + ", ".join(itertools.chain((self.dummy_name,), self.names.values()))
            + "]>"
        )
        return string
