"""Load and validate the customer requirement file (YAML).

The requirement file describes one PV site and the BESS options that should be
evaluated for it. Everything the model needs is in this one file so that a
study can be reproduced exactly from version control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Location:
    name: str
    lat: float
    lon: float


@dataclass
class Project:
    name: str
    customer: str
    location: Location
    use_case: str = "PV shifting and price arbitrage"


@dataclass
class Site:
    pv_dc_mwp: float
    pv_ac_mw: float
    grid_export_limit_mw: float
    grid_import_limit_mw: float = 0.0
    allow_grid_charging: bool = True
    pv_tilt_deg: float = 25.0
    pv_azimuth_deg: float = 180.0  # pvlib convention, 180 = south
    pv_system_loss_pct: float = 14.0


@dataclass
class DataSources:
    year: int
    pv_profile_csv: Path
    price_csv: Path
    label: str = "unspecified"  # shown in the report, e.g. "PVGIS-SARAH3 + SMARD 2023"
    synthetic: bool = False


@dataclass
class Battery:
    roundtrip_efficiency: float = 0.88
    soc_min: float = 0.05
    soc_max: float = 0.95
    soc_initial: float = 0.5
    max_cycles_per_day: float | None = 1.5
    degradation_cost_eur_per_mwh: float = 5.0
    annual_capacity_fade: float = 0.02


@dataclass
class Economics:
    capex_energy_eur_per_kwh: float = 180.0
    capex_power_eur_per_kw: float = 70.0
    opex_pct_of_capex: float = 0.015
    grid_charging_fee_eur_per_mwh: float = 0.0
    project_life_years: int = 15
    discount_rate: float = 0.07


@dataclass
class Options:
    power_mw: list[float]
    duration_h: list[float]
    rank_by: str = "npv"  # npv | irr | payback


@dataclass
class StudyConfig:
    project: Project
    site: Site
    data: DataSources
    options: Options
    battery: Battery = field(default_factory=Battery)
    economics: Economics = field(default_factory=Economics)
    catalogue_path: Path | None = None
    source_file: Path | None = None


class ConfigError(ValueError):
    """Raised when the requirement file is incomplete or inconsistent."""


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"Missing '{key}' in section '{where}'")
    return d[key]


def _resolve(base: Path, p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (base / p).resolve()


def load_config(path: str | Path) -> StudyConfig:
    """Read a requirement YAML file. Relative paths are resolved against the
    repository root (the folder that contains ``pyproject.toml``) if one is
    found above the file, otherwise against the file's own folder."""
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = _find_repo_root(path.parent)
    return config_from_dict(raw, base=base, source_file=path)


def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").exists():
            return p
    return start


def config_from_dict(raw: dict[str, Any], base: Path, source_file: Path | None = None) -> StudyConfig:
    p = _require(raw, "project", "root")
    loc = _require(p, "location", "project")
    project = Project(
        name=_require(p, "name", "project"),
        customer=p.get("customer", "n/a"),
        location=Location(
            name=loc.get("name", ""),
            lat=float(_require(loc, "lat", "project.location")),
            lon=float(_require(loc, "lon", "project.location")),
        ),
        use_case=p.get("use_case", "PV shifting and price arbitrage"),
    )

    s = _require(raw, "site", "root")
    site = Site(
        pv_dc_mwp=float(_require(s, "pv_dc_mwp", "site")),
        pv_ac_mw=float(_require(s, "pv_ac_mw", "site")),
        grid_export_limit_mw=float(_require(s, "grid_export_limit_mw", "site")),
        grid_import_limit_mw=float(s.get("grid_import_limit_mw", 0.0)),
        allow_grid_charging=bool(s.get("allow_grid_charging", True)),
        pv_tilt_deg=float(s.get("pv_tilt_deg", 25.0)),
        pv_azimuth_deg=float(s.get("pv_azimuth_deg", 180.0)),
        pv_system_loss_pct=float(s.get("pv_system_loss_pct", 14.0)),
    )

    d = _require(raw, "data", "root")
    data = DataSources(
        year=int(_require(d, "year", "data")),
        pv_profile_csv=_resolve(base, _require(d, "pv_profile_csv", "data")),
        price_csv=_resolve(base, _require(d, "price_csv", "data")),
        label=d.get("label", "unspecified"),
        synthetic=bool(d.get("synthetic", False)),
    )

    o = _require(raw, "options", "root")
    options = Options(
        power_mw=[float(x) for x in _require(o, "power_mw", "options")],
        duration_h=[float(x) for x in _require(o, "duration_h", "options")],
        rank_by=o.get("rank_by", "npv"),
    )

    battery = Battery(**raw.get("battery", {}))
    economics = Economics(**raw.get("economics", {}))
    cat = raw.get("catalogue")

    cfg = StudyConfig(
        project=project,
        site=site,
        data=data,
        options=options,
        battery=battery,
        economics=economics,
        catalogue_path=_resolve(base, cat) if cat else None,
        source_file=source_file,
    )
    validate(cfg)
    return cfg


def validate(cfg: StudyConfig) -> None:
    s, b, o = cfg.site, cfg.battery, cfg.options
    if s.pv_ac_mw <= 0 or s.pv_dc_mwp <= 0:
        raise ConfigError("PV size must be positive")
    if s.pv_ac_mw > s.pv_dc_mwp * 1.05:
        raise ConfigError("pv_ac_mw is larger than pv_dc_mwp; check the DC/AC ratio")
    if s.grid_export_limit_mw <= 0:
        raise ConfigError("grid_export_limit_mw must be positive")
    if s.allow_grid_charging and s.grid_import_limit_mw <= 0:
        raise ConfigError("allow_grid_charging is true but grid_import_limit_mw is 0")
    if not 0 < b.roundtrip_efficiency <= 1:
        raise ConfigError("roundtrip_efficiency must be in (0, 1]")
    if not 0 <= b.soc_min < b.soc_max <= 1:
        raise ConfigError("soc_min / soc_max must satisfy 0 <= soc_min < soc_max <= 1")
    if not b.soc_min <= b.soc_initial <= b.soc_max:
        raise ConfigError("soc_initial must lie between soc_min and soc_max")
    if not o.power_mw or not o.duration_h:
        raise ConfigError("options.power_mw and options.duration_h must not be empty")
    if any(x <= 0 for x in o.power_mw + o.duration_h):
        raise ConfigError("BESS power and duration options must be positive")
    if o.rank_by not in {"npv", "irr", "payback"}:
        raise ConfigError("options.rank_by must be one of npv, irr, payback")
