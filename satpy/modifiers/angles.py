#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2021 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""Utilties for getting various angles for a dataset.."""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from functools import update_wrapper
from glob import glob
from typing import Any, Callable, Optional, Union, cast

import dask
import dask.array as da
import numpy as np
import xarray as xr
from pyorbital.astronomy import get_alt_az, sun_zenith_angle
from pyorbital.orbital import get_observer_look
from pyresample.geometry import AreaDefinition, SwathDefinition

import satpy
from satpy.utils import get_satpos, ignore_invalid_float_warnings

PRGeometry = Union[SwathDefinition, AreaDefinition]

# Arbitrary time used when computing sensor angles that is passed to
# pyorbital's get_observer_look function.
# The difference is on the order of 1e-10 at most as time changes so we force
# it to a single time for easier caching. It is *only* used if caching.
STATIC_EARTH_INERTIAL_DATETIME = datetime(2000, 1, 1, 12, 0, 0)


class ZarrCacheHelper:
    """Helper for caching function results to on-disk zarr arrays.

    It is recommended to use this class through the :func:`cache_to_zarr_if`
    decorator rather than using it directly.

    Currently the cache does not perform any limiting or removal of cache
    content. That is left up to the user to manage. Caching is based on
    arguments passed to the decorated function but will only be performed
    if the arguments are of a certain type (see ``uncacheable_arg_types``).
    The cache value to use is purely based on the hash value of all of the
    provided arguments along with the "cache version" (see below).

    Args:
        func: Function that will be called to generate the value to cache.
        cache_config_key: Name of the boolean ``satpy.config`` parameter to
            use to determine if caching should be done.
        uncacheable_arg_types: Types that if present in the passed arguments
            should trigger caching to *not* happen. By default this includes
            ``SwathDefinition``, ``xr.DataArray``, and ``da.Array`` objects.
        sanitize_args_func: Optional function to call to sanitize provided
            arguments before they are considered for caching. This can be used
            to make arguments more "cacheable" by replacing them with similar
            values that will result in more cache hits. Note that the sanitized
            arguments are only passed to the underlying function if caching
            will be performed, otherwise the original arguments are passed.
        cache_version: Version number used to distinguish one version of a
            decorated function from future versions.

    Notes:
        * Caching only supports dask array values.

        * This helper allows for an additional ``cache_dir`` parameter to
          override the use of the ``satpy.config`` ``cache_dir`` parameter.

    Examples:
        To use through the :func:`cache_to_zarr_if` decorator::

            @cache_to_zarr_if("cache_my_stuff")
            def generate_my_stuff(area_def: AreaDefinition, some_factor: int) -> da.Array:
                # Generate
                return my_dask_arr

        To use the decorated function::

            with satpy.config.set(cache_my_stuff=True):
                my_stuff = generate_my_stuff(area_def, 5)

    """

    def __init__(self,
                 func: Callable,
                 cache_config_key: str,
                 uncacheable_arg_types=(SwathDefinition, xr.DataArray, da.Array),
                 sanitize_args_func: Callable = None,
                 cache_version: int = 1,
                 ):
        """Hold on to provided arguments for future use."""
        self._func = func
        self._cache_config_key = cache_config_key
        self._uncacheable_arg_types = uncacheable_arg_types
        self._sanitize_args_func = sanitize_args_func
        self._cache_version = cache_version

    def cache_clear(self, cache_dir: Optional[str] = None):
        """Remove all on-disk files associated with this function.

        Intended to mimic the :func:`functools.cache` behavior.
        """
        if cache_dir is None:
            cache_dir = satpy.config.get("cache_dir")
        if cache_dir is None:
            raise RuntimeError("No 'cache_dir' configured.")
        zarr_pattern = self._zarr_pattern("*", cache_version="*").format("*")
        for zarr_dir in glob(os.path.join(cache_dir, zarr_pattern)):
            try:
                shutil.rmtree(zarr_dir)
            except OSError:
                continue

    def _zarr_pattern(self, arg_hash, cache_version: Union[int, str] = None) -> str:
        if cache_version is None:
            cache_version = self._cache_version
        return f"{self._func.__name__}_v{cache_version}" + "_{}_" + f"{arg_hash}.zarr"

    def __call__(self, *args, cache_dir: Optional[str] = None) -> Any:
        """Call the decorated function."""
        new_args = self._sanitize_args_func(*args) if self._sanitize_args_func is not None else args
        arg_hash = _hash_args(*new_args)
        should_cache, cache_dir = self._get_should_cache_and_cache_dir(new_args, cache_dir)
        zarr_fn = self._zarr_pattern(arg_hash)
        zarr_format = os.path.join(cache_dir, zarr_fn)
        zarr_paths = glob(zarr_format.format("*"))
        if not should_cache or not zarr_paths:
            # use sanitized arguments if we are caching, otherwise use original arguments
            args = new_args if should_cache else args
            res = self._func(*args)
            if should_cache and not zarr_paths:
                self._cache_results(res, zarr_format)
        # if we did any caching, let's load from the zarr files
        if should_cache:
            # re-calculate the cached paths
            zarr_paths = glob(zarr_format.format("*"))
            if not zarr_paths:
                raise RuntimeError("Data was cached to disk but no files were found")
            res = tuple(da.from_zarr(zarr_path) for zarr_path in zarr_paths)
        return res

    def _get_should_cache_and_cache_dir(self, args, cache_dir: Optional[str]) -> tuple[bool, str]:
        should_cache: bool = satpy.config.get(self._cache_config_key, False)
        can_cache = not any(isinstance(arg, self._uncacheable_arg_types) for arg in args)
        should_cache = should_cache and can_cache
        if cache_dir is None:
            cache_dir = satpy.config.get("cache_dir")
        if cache_dir is None:
            should_cache = False
        cache_dir = cast(str, cache_dir)
        return should_cache, cache_dir

    def _cache_results(self, res, zarr_format):
        os.makedirs(os.path.dirname(zarr_format), exist_ok=True)
        new_res = []
        for idx, sub_res in enumerate(res):
            if not isinstance(sub_res, da.Array):
                raise ValueError("Zarr caching currently only supports dask "
                                 f"arrays. Got {type(sub_res)}")
            zarr_path = zarr_format.format(idx)
            # See https://github.com/dask/dask/issues/8380
            with dask.config.set({"optimization.fuse.active": False}):
                new_sub_res = sub_res.to_zarr(zarr_path,
                                              return_stored=True,
                                              compute=False)
            new_res.append(new_sub_res)
        # actually compute the storage to zarr
        da.compute(new_res)


def cache_to_zarr_if(
        cache_config_key: str,
        uncacheable_arg_types=(SwathDefinition, xr.DataArray, da.Array),
        sanitize_args_func: Callable = None,
) -> Callable:
    """Decorate a function and cache the results as a zarr array on disk.

    This only happens if the ``satpy.config`` boolean value for the provided
    key is ``True`` as well as some other conditions. See
    :class:`ZarrCacheHelper` for more information. Most importantly, this
    decorator does not limit how many items can be cached and does not clear
    out old entries. It is up to the user to manage the size of the cache.

    """
    def _decorator(func: Callable) -> Callable:
        zarr_cacher = ZarrCacheHelper(func,
                                      cache_config_key,
                                      uncacheable_arg_types,
                                      sanitize_args_func)
        wrapper = update_wrapper(zarr_cacher, func)
        return wrapper
    return _decorator


def _hash_args(*args):
    import json
    hashable_args = []
    for arg in args:
        if isinstance(arg, (xr.DataArray, da.Array, SwathDefinition)):
            continue
        if isinstance(arg, AreaDefinition):
            arg = hash(arg)
        elif isinstance(arg, datetime):
            arg = arg.isoformat(" ")
        hashable_args.append(arg)
    arg_hash = hashlib.sha1()
    arg_hash.update(json.dumps(tuple(hashable_args)).encode('utf8'))
    return arg_hash.hexdigest()


def _sanitize_observer_look_args(*args):
    new_args = []
    for arg in args:
        if isinstance(arg, datetime):
            new_args.append(STATIC_EARTH_INERTIAL_DATETIME)
        elif isinstance(arg, (float, np.float64, np.float32)):
            # round floating point numbers to nearest tenth
            new_args.append(round(arg, 1))
        else:
            new_args.append(arg)
    return new_args


def _geo_dask_to_data_array(arr: da.Array) -> xr.DataArray:
    return xr.DataArray(arr, dims=('y', 'x'))


def get_angles(data_arr: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray, xr.DataArray, xr.DataArray]:
    """Get sun and satellite azimuth and zenith angles.

    Note that this function can benefit from the ``satpy.config`` parameters
    :ref:`cache_lonlats <config_cache_lonlats_setting>` and
    :ref:`cache_sensor_angles <config_cache_sensor_angles_setting>`
    being set to ``True``.

    Args:
        data_arr: DataArray to get angles for. Information extracted from this
            object are ``.attrs["area"]`` and ``.attrs["start_time"]``.
            Additionally, the dask array chunk size is used when generating
            new arrays. The actual data of the object is not used.

    Returns:
        Four DataArrays representing sensor azimuth angle, sensor zenith angle,
        solar azimuth angle, and solar zenith angle.

    """
    sata, satz = _get_sensor_angles(data_arr)
    suna, sunz = _get_sun_angles(data_arr)
    return sata, satz, suna, sunz


def get_satellite_zenith_angle(data_arr: xr.DataArray) -> xr.DataArray:
    """Generate satellite zenith angle for the provided data.

    Note that this function can benefit from the ``satpy.config`` parameters
    :ref:`cache_lonlats <config_cache_lonlats_setting>` and
    :ref:`cache_sensor_angles <config_cache_sensor_angles_setting>`
    being set to ``True``.

    """
    satz = _get_sensor_angles(data_arr)[1]
    return satz


@cache_to_zarr_if("cache_lonlats")
def _get_valid_lonlats(area: PRGeometry, chunks: Union[int, str] = "auto") -> tuple[da.Array, da.Array]:
    with ignore_invalid_float_warnings():
        lons, lats = area.get_lonlats(chunks=chunks)
        lons = da.where(lons >= 1e30, np.nan, lons)
        lats = da.where(lats >= 1e30, np.nan, lats)
    return lons, lats


def _get_sun_angles(data_arr: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    lons, lats = _get_valid_lonlats(data_arr.attrs["area"], data_arr.data.chunks)
    res = da.map_blocks(_get_sun_angles_wrapper, lons, lats,
                        data_arr.attrs["start_time"],
                        dtype=lons.dtype, meta=np.array((), dtype=lons.dtype),
                        new_axis=[0], chunks=(2,) + lons.chunks)
    suna = _geo_dask_to_data_array(res[0])
    sunz = _geo_dask_to_data_array(res[1])
    return suna, sunz


def _get_sun_angles_wrapper(lons: da.Array, lats: da.Array, start_time: datetime) -> tuple[da.Array, da.Array]:
    with ignore_invalid_float_warnings():
        suna = get_alt_az(start_time, lons, lats)[1]
        suna = np.rad2deg(suna)
        sunz = sun_zenith_angle(start_time, lons, lats)
        return np.stack([suna, sunz])


def _get_sensor_angles(data_arr: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    sat_lon, sat_lat, sat_alt = get_satpos(data_arr)
    area_def = data_arr.attrs["area"]
    sata, satz = _get_sensor_angles_from_sat_pos(sat_lon, sat_lat, sat_alt,
                                                 data_arr.attrs["start_time"],
                                                 area_def, data_arr.data.chunks)
    sata = _geo_dask_to_data_array(sata)
    satz = _geo_dask_to_data_array(satz)
    return sata, satz


@cache_to_zarr_if("cache_sensor_angles", sanitize_args_func=_sanitize_observer_look_args)
def _get_sensor_angles_from_sat_pos(sat_lon, sat_lat, sat_alt, start_time, area_def, chunks):
    lons, lats = _get_valid_lonlats(area_def, chunks)
    res = da.map_blocks(_get_sensor_angles_wrapper, lons, lats, start_time, sat_lon, sat_lat, sat_alt,
                        dtype=lons.dtype, meta=np.array((), dtype=lons.dtype), new_axis=[0],
                        chunks=(2,) + lons.chunks)
    return res[0], res[1]


def _get_sensor_angles_wrapper(lons, lats, start_time, sat_lon, sat_lat, sat_alt):
    with ignore_invalid_float_warnings():
        sata, satel = get_observer_look(
            sat_lon,
            sat_lat,
            sat_alt / 1000.0,  # km
            start_time,
            lons, lats, 0)
        satz = 90 - satel
        return np.stack([sata, satz])
