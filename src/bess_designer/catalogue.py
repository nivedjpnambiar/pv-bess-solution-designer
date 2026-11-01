"""Turn a target (MW, hours) into a concrete configuration from a product catalogue.

The default catalogue in ``catalogue/generic_catalogue.yaml`` is vendor-neutral
and only illustrative. Swap in real datasheet values to configure a specific
product family.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class BatteryUnit:
    id: str
    description: str
    nameplate_mwh: float


@dataclass
class PcsUnit:
    id: str
    description: str
    rated_mw: float


@dataclass
class Catalogue:
    battery_units: list[BatteryUnit]
    pcs_units: list[PcsUnit]


@dataclass
class Configuration:
    target_power_mw: float
    target_duration_h: float
    battery_unit: BatteryUnit
    n_battery_units: int
    pcs_unit: PcsUnit
    n_pcs_units: int
    power_mw: float
    nameplate_mwh: float
    usable_mwh: float

    @property
    def duration_h(self) -> float:
        return self.usable_mwh / self.power_mw

    @property
    def label(self) -> str:
        return f"{self.target_power_mw:g} MW / {self.target_duration_h:g} h"


DEFAULT_CATALOGUE = Catalogue(
    battery_units=[BatteryUnit("BC-5000", "20 ft LFP battery container, liquid cooled (generic)", 5.0)],
    pcs_units=[PcsUnit("PCS-2500", "Bidirectional PCS skid with MV transformer (generic)", 2.5)],
)


def load_catalogue(path: str | Path | None) -> Catalogue:
    if path is None:
        return DEFAULT_CATALOGUE
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Catalogue(
        battery_units=[BatteryUnit(**u) for u in raw["battery_units"]],
        pcs_units=[PcsUnit(**u) for u in raw["pcs_units"]],
    )


def configure(
    power_mw: float,
    duration_h: float,
    catalogue: Catalogue,
    soc_min: float,
    soc_max: float,
) -> Configuration:
    """Pick the unit combination with the least oversizing that meets the target.

    ``duration_h`` refers to *usable* energy (between soc_min and soc_max), which
    is how utility customers usually specify a system ("20 MW / 2 h").
    """
    window = soc_max - soc_min
    needed_nameplate = power_mw * duration_h / window

    best_b, n_b = min(
        ((u, math.ceil(needed_nameplate / u.nameplate_mwh - 1e-9)) for u in catalogue.battery_units),
        key=lambda x: x[1] * x[0].nameplate_mwh,
    )
    best_p, n_p = min(
        ((u, math.ceil(power_mw / u.rated_mw - 1e-9)) for u in catalogue.pcs_units),
        key=lambda x: x[1] * x[0].rated_mw,
    )
    nameplate = n_b * best_b.nameplate_mwh
    return Configuration(
        target_power_mw=power_mw,
        target_duration_h=duration_h,
        battery_unit=best_b,
        n_battery_units=n_b,
        pcs_unit=best_p,
        n_pcs_units=n_p,
        power_mw=n_p * best_p.rated_mw,
        nameplate_mwh=nameplate,
        usable_mwh=nameplate * window,
    )
