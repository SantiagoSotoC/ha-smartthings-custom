"""Support for sensors through the SmartThings cloud API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from pysmartthings import Attribute, Capability, ComponentStatus, SmartThings, Status

from homeassistant.components.automation import automations_with_entity
from homeassistant.components.script import scripts_with_entity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfEnergy,
    UnitOfMass,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.util import dt as dt_util

from . import FullDevice, SmartThingsConfigEntry
from .const import DOMAIN, MAIN
from .entity import SmartThingsEntity

THERMOSTAT_CAPABILITIES = {
    Capability.TEMPERATURE_MEASUREMENT,
    Capability.THERMOSTAT_HEATING_SETPOINT,
    Capability.THERMOSTAT_MODE,
}

JOB_STATE_MAP = {
    "airWash": "air_wash",
    "airwash": "air_wash",
    "aIRinse": "ai_rinse",
    "aISpin": "ai_spin",
    "aIWash": "ai_wash",
    "aIDrying": "ai_drying",
    "internalCare": "internal_care",
    "continuousDehumidifying": "continuous_dehumidifying",
    "thawingFrozenInside": "thawing_frozen_inside",
    "delayWash": "delay_wash",
    "weightSensing": "weight_sensing",
    "freezeProtection": "freeze_protection",
    "preDrain": "pre_drain",
    "preWash": "pre_wash",
    "prewash": "pre_wash",
    "wrinklePrevent": "wrinkle_prevent",
    "unknown": None,
}

OVEN_JOB_STATE_MAP = {
    "scheduledStart": "scheduled_start",
    "fastPreheat": "fast_preheat",
    "scheduledEnd": "scheduled_end",
    "stone_heating": "stone_heating",
    "timeHoldPreheat": "time_hold_preheat",
}

MEDIA_PLAYBACK_STATE_MAP = {
    "fast forwarding": "fast_forwarding",
}

ROBOT_CLEANER_TURBO_MODE_STATE_MAP = {
    "extraSilence": "extra_silence",
}

ROBOT_CLEANER_MOVEMENT_MAP = {
    "powerOff": "off",
}

OVEN_MODE = {
    "Conventional": "conventional",
    "Bake": "bake",
    "BottomHeat": "bottom_heat",
    "ConvectionBake": "convection_bake",
    "ConvectionRoast": "convection_roast",
    "Broil": "broil",
    "ConvectionBroil": "convection_broil",
    "SteamCook": "steam_cook",
    "SteamBake": "steam_bake",
    "SteamRoast": "steam_roast",
    "SteamBottomHeatplusConvection": "steam_bottom_heat_plus_convection",
    "Microwave": "microwave",
    "MWplusGrill": "microwave_plus_grill",
    "MWplusConvection": "microwave_plus_convection",
    "MWplusHotBlast": "microwave_plus_hot_blast",
    "MWplusHotBlast2": "microwave_plus_hot_blast_2",
    "SlimMiddle": "slim_middle",
    "SlimStrong": "slim_strong",
    "SlowCook": "slow_cook",
    "Proof": "proof",
    "Dehydrate": "dehydrate",
    "Others": "others",
    "StrongSteam": "strong_steam",
    "Descale": "descale",
    "Rinse": "rinse",
}

WASHER_OPTIONS = ["pause", "run", "stop"]


def power_attributes(status: dict[str, Any]) -> dict[str, Any]:
    """Return the power attributes."""
    state = {}
    for attribute in ("start", "end"):
        if (value := status.get(attribute)) is not None:
            state[f"power_consumption_{attribute}"] = value
    return state


@dataclass(frozen=True, kw_only=True)
class SmartThingsSensorEntityDescription(SensorEntityDescription):
    """Describe a SmartThings sensor entity."""

    value_fn: Callable[[Any], str | float | int | datetime | None] = lambda value: value
    extra_state_attributes_fn: Callable[[Any], dict[str, Any]] | None = None
    capability_ignore_list: list[set[Capability]] | None = None
    options_attribute: Attribute | None = None
    exists_fn: Callable[[Status], bool] | None = None
    use_temperature_unit: bool = False
    deprecated: Callable[[ComponentStatus], str | None] | None = None


CAPABILITY_TO_SENSORS: dict[
    Capability, dict[Attribute, list[SmartThingsSensorEntityDescription]]
] = {
    # Haven't seen at devices yet
    Capability.ACTIVITY_LIGHTING_MODE: {
        Attribute.LIGHTING_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.LIGHTING_MODE,
                translation_key="lighting_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    Capability.AIR_CONDITIONER_MODE: {
        Attribute.AIR_CONDITIONER_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.AIR_CONDITIONER_MODE,
                translation_key="air_conditioner_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
                capability_ignore_list=[
                    {
                        Capability.TEMPERATURE_MEASUREMENT,
                        Capability.THERMOSTAT_COOLING_SETPOINT,
                    }
                ],
            )
        ]
    },
    Capability.AIR_QUALITY_SENSOR: {
        Attribute.AIR_QUALITY: [
            SmartThingsSensorEntityDescription(
                key=Attribute.AIR_QUALITY,
                translation_key="air_quality",
                native_unit_of_measurement="CAQI",
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.ALARM: {
        Attribute.ALARM: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ALARM,
                translation_key="alarm",
                options=["both", "strobe", "siren", "off"],
                device_class=SensorDeviceClass.ENUM,
            )
        ]
    },
    Capability.AUDIO_VOLUME: {
        Attribute.VOLUME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.VOLUME,
                translation_key="audio_volume",
                native_unit_of_measurement=PERCENTAGE,
                deprecated=(
                    lambda status: "media_player"
                    if all(
                        capability in status
                        for capability in (
                            Capability.AUDIO_MUTE,
                            Capability.MEDIA_PLAYBACK,
                        )
                    )
                    else None
                ),
            )
        ]
    },
    Capability.BATTERY: {
        Attribute.BATTERY: [
            SmartThingsSensorEntityDescription(
                key=Attribute.BATTERY,
                native_unit_of_measurement=PERCENTAGE,
                device_class=SensorDeviceClass.BATTERY,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.BODY_MASS_INDEX_MEASUREMENT: {
        Attribute.BMI_MEASUREMENT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.BMI_MEASUREMENT,
                translation_key="body_mass_index",
                native_unit_of_measurement=f"{UnitOfMass.KILOGRAMS}/{UnitOfArea.SQUARE_METERS}",
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.BODY_WEIGHT_MEASUREMENT: {
        Attribute.BODY_WEIGHT_MEASUREMENT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.BODY_WEIGHT_MEASUREMENT,
                translation_key="body_weight",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                device_class=SensorDeviceClass.WEIGHT,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.CARBON_DIOXIDE_MEASUREMENT: {
        Attribute.CARBON_DIOXIDE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.CARBON_DIOXIDE,
                native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                device_class=SensorDeviceClass.CO2,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.CARBON_MONOXIDE_DETECTOR: {
        Attribute.CARBON_MONOXIDE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.CARBON_MONOXIDE,
                translation_key="carbon_monoxide_detector",
                options=["detected", "clear", "tested"],
                device_class=SensorDeviceClass.ENUM,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.CARBON_MONOXIDE_MEASUREMENT: {
        Attribute.CARBON_MONOXIDE_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.CARBON_MONOXIDE_LEVEL,
                native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                device_class=SensorDeviceClass.CO,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.DISHWASHER_OPERATING_STATE: {
        Attribute.MACHINE_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.MACHINE_STATE,
                translation_key="dishwasher_machine_state",
                options=WASHER_OPTIONS,
                device_class=SensorDeviceClass.ENUM,
            )
        ],
        Attribute.DISHWASHER_JOB_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.DISHWASHER_JOB_STATE,
                translation_key="dishwasher_job_state",
                options=[
                    "air_wash",
                    "cooling",
                    "drying",
                    "finish",
                    "pre_drain",
                    "pre_wash",
                    "rinse",
                    "spin",
                    "wash",
                    "wrinkle_prevent",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: JOB_STATE_MAP.get(value, value),
            )
        ],
        Attribute.COMPLETION_TIME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.COMPLETION_TIME,
                translation_key="completion_time",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=dt_util.parse_datetime,
            )
        ],
    },
    # part of the proposed spec, Haven't seen at devices yet
    Capability.DRYER_MODE: {
        Attribute.DRYER_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.DRYER_MODE,
                translation_key="dryer_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    Capability.DRYER_OPERATING_STATE: {
        Attribute.MACHINE_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.MACHINE_STATE,
                translation_key="dryer_machine_state",
                options=WASHER_OPTIONS,
                device_class=SensorDeviceClass.ENUM,
            )
        ],
        Attribute.DRYER_JOB_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.DRYER_JOB_STATE,
                translation_key="dryer_job_state",
                options=[
                    "cooling",
                    "delay_wash",
                    "drying",
                    "finished",
                    "none",
                    "refreshing",
                    "weight_sensing",
                    "wrinkle_prevent",
                    "dehumidifying",
                    "ai_drying",
                    "sanitizing",
                    "internal_care",
                    "freeze_protection",
                    "continuous_dehumidifying",
                    "thawing_frozen_inside",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: JOB_STATE_MAP.get(value, value),
            )
        ],
        Attribute.COMPLETION_TIME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.COMPLETION_TIME,
                translation_key="completion_time",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=dt_util.parse_datetime,
            )
        ],
    },
    Capability.DUST_SENSOR: {
        Attribute.DUST_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.DUST_LEVEL,
                device_class=SensorDeviceClass.PM10,
                native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ],
        Attribute.FINE_DUST_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.FINE_DUST_LEVEL,
                device_class=SensorDeviceClass.PM25,
                native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ],
    },
    Capability.ENERGY_METER: {
        Attribute.ENERGY: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL_INCREASING,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.EQUIVALENT_CARBON_DIOXIDE_MEASUREMENT: {
        Attribute.EQUIVALENT_CARBON_DIOXIDE_MEASUREMENT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.EQUIVALENT_CARBON_DIOXIDE_MEASUREMENT,
                translation_key="equivalent_carbon_dioxide",
                native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                device_class=SensorDeviceClass.CO2,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.FORMALDEHYDE_MEASUREMENT: {
        Attribute.FORMALDEHYDE_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.FORMALDEHYDE_LEVEL,
                translation_key="formaldehyde",
                native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.GAS_METER: {
        Attribute.GAS_METER: [
            SmartThingsSensorEntityDescription(
                key=Attribute.GAS_METER,
                translation_key="gas_meter",
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ],
        Attribute.GAS_METER_CALORIFIC: [
            SmartThingsSensorEntityDescription(
                key=Attribute.GAS_METER_CALORIFIC,
                translation_key="gas_meter_calorific",
            )
        ],
        Attribute.GAS_METER_TIME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.GAS_METER_TIME,
                translation_key="gas_meter_time",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=dt_util.parse_datetime,
            )
        ],
        Attribute.GAS_METER_VOLUME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.GAS_METER_VOLUME,
                native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
                device_class=SensorDeviceClass.GAS,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ],
    },
    # Haven't seen at devices yet
    Capability.ILLUMINANCE_MEASUREMENT: {
        Attribute.ILLUMINANCE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ILLUMINANCE,
                native_unit_of_measurement=LIGHT_LUX,
                device_class=SensorDeviceClass.ILLUMINANCE,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.INFRARED_LEVEL: {
        Attribute.INFRARED_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.INFRARED_LEVEL,
                translation_key="infrared_level",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.MEDIA_INPUT_SOURCE: {
        Attribute.INPUT_SOURCE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.INPUT_SOURCE,
                translation_key="media_input_source",
                device_class=SensorDeviceClass.ENUM,
                options_attribute=Attribute.SUPPORTED_INPUT_SOURCES,
                value_fn=lambda value: value.lower() if value else None,
                deprecated=lambda _: "media_player",
            )
        ]
    },
    Capability.MEDIA_PLAYBACK_REPEAT: {
        Attribute.PLAYBACK_REPEAT_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.PLAYBACK_REPEAT_MODE,
                translation_key="media_playback_repeat",
                deprecated=lambda _: "media_player",
            )
        ]
    },
    Capability.MEDIA_PLAYBACK_SHUFFLE: {
        Attribute.PLAYBACK_SHUFFLE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.PLAYBACK_SHUFFLE,
                translation_key="media_playback_shuffle",
                deprecated=lambda _: "media_player",
            )
        ]
    },
    Capability.MEDIA_PLAYBACK: {
        Attribute.PLAYBACK_STATUS: [
            SmartThingsSensorEntityDescription(
                key=Attribute.PLAYBACK_STATUS,
                translation_key="media_playback_status",
                options=[
                    "paused",
                    "playing",
                    "stopped",
                    "fast_forwarding",
                    "rewinding",
                    "buffering",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: MEDIA_PLAYBACK_STATE_MAP.get(value, value),
                deprecated=lambda _: "media_player",
            )
        ]
    },
    Capability.ODOR_SENSOR: {
        Attribute.ODOR_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ODOR_LEVEL,
                translation_key="odor_sensor",
            )
        ]
    },
    Capability.OVEN_MODE: {
        Attribute.OVEN_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.OVEN_MODE,
                translation_key="oven_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
                options=list(OVEN_MODE.values()),
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: OVEN_MODE.get(value, value),
            )
        ]
    },
    Capability.OVEN_OPERATING_STATE: {
        Attribute.MACHINE_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.MACHINE_STATE,
                translation_key="oven_machine_state",
                options=["ready", "running", "paused"],
                device_class=SensorDeviceClass.ENUM,
            )
        ],
        Attribute.OVEN_JOB_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.OVEN_JOB_STATE,
                translation_key="oven_job_state",
                options=[
                    "cleaning",
                    "cooking",
                    "cooling",
                    "draining",
                    "preheat",
                    "ready",
                    "rinsing",
                    "finished",
                    "scheduled_start",
                    "warming",
                    "defrosting",
                    "sensing",
                    "searing",
                    "fast_preheat",
                    "scheduled_end",
                    "stone_heating",
                    "time_hold_preheat",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: OVEN_JOB_STATE_MAP.get(value, value),
            )
        ],
        Attribute.COMPLETION_TIME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.COMPLETION_TIME,
                translation_key="completion_time",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=dt_util.parse_datetime,
            )
        ],
    },
    Capability.OVEN_SETPOINT: {
        Attribute.OVEN_SETPOINT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.OVEN_SETPOINT,
                translation_key="oven_setpoint",
                device_class=SensorDeviceClass.TEMPERATURE,
                use_temperature_unit=True,
                # Set the value to None if it is 0 F (-17 C)
                value_fn=lambda value: None if value in {0, -17} else value,
            )
        ]
    },
    Capability.POWER_CONSUMPTION_REPORT: {
        Attribute.POWER_CONSUMPTION: [
            SmartThingsSensorEntityDescription(
                key="energy_meter",
                state_class=SensorStateClass.TOTAL_INCREASING,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                value_fn=lambda value: value["energy"] / 1000,
                suggested_display_precision=2,
                exists_fn=lambda status: (
                    (value := cast(dict | None, status.value)) is not None
                    and "energy" in value
                ),
            ),
            SmartThingsSensorEntityDescription(
                key="power_meter",
                state_class=SensorStateClass.MEASUREMENT,
                device_class=SensorDeviceClass.POWER,
                native_unit_of_measurement=UnitOfPower.WATT,
                value_fn=lambda value: value["power"],
                extra_state_attributes_fn=power_attributes,
                suggested_display_precision=2,
                exists_fn=lambda status: (
                    (value := cast(dict | None, status.value)) is not None
                    and "power" in value
                ),
            ),
            SmartThingsSensorEntityDescription(
                key="deltaEnergy_meter",
                translation_key="energy_difference",
                state_class=SensorStateClass.TOTAL,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                value_fn=lambda value: value["deltaEnergy"] / 1000,
                suggested_display_precision=2,
                exists_fn=lambda status: (
                    (value := cast(dict | None, status.value)) is not None
                    and "deltaEnergy" in value
                ),
            ),
            SmartThingsSensorEntityDescription(
                key="powerEnergy_meter",
                translation_key="power_energy",
                state_class=SensorStateClass.TOTAL_INCREASING,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                value_fn=lambda value: value["powerEnergy"] / 1000,
                suggested_display_precision=2,
                exists_fn=lambda status: (
                    (value := cast(dict | None, status.value)) is not None
                    and "powerEnergy" in value
                ),
            ),
            SmartThingsSensorEntityDescription(
                key="energySaved_meter",
                translation_key="energy_saved",
                state_class=SensorStateClass.TOTAL_INCREASING,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                value_fn=lambda value: value["energySaved"] / 1000,
                suggested_display_precision=2,
                exists_fn=lambda status: (
                    (value := cast(dict | None, status.value)) is not None
                    and "energySaved" in value
                ),
            ),
        ]
    },
    Capability.POWER_METER: {
        Attribute.POWER: [
            SmartThingsSensorEntityDescription(
                key=Attribute.POWER,
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.POWER_SOURCE: {
        Attribute.POWER_SOURCE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.POWER_SOURCE,
                translation_key="power_source",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    # part of the proposed spec
    Capability.REFRIGERATION_SETPOINT: {
        Attribute.REFRIGERATION_SETPOINT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.REFRIGERATION_SETPOINT,
                translation_key="refrigeration_setpoint",
                device_class=SensorDeviceClass.TEMPERATURE,
            )
        ]
    },
    Capability.RELATIVE_BRIGHTNESS: {
        Attribute.BRIGHTNESS_INTENSITY: [
            SmartThingsSensorEntityDescription(
                key=Attribute.BRIGHTNESS_INTENSITY,
                translation_key="brightness_intensity",
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.RELATIVE_HUMIDITY_MEASUREMENT: {
        Attribute.HUMIDITY: [
            SmartThingsSensorEntityDescription(
                key=Attribute.HUMIDITY,
                native_unit_of_measurement=PERCENTAGE,
                device_class=SensorDeviceClass.HUMIDITY,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.ROBOT_CLEANER_CLEANING_MODE: {
        Attribute.ROBOT_CLEANER_CLEANING_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ROBOT_CLEANER_CLEANING_MODE,
                translation_key="robot_cleaner_cleaning_mode",
                options=["auto", "part", "repeat", "manual", "stop", "map"],
                device_class=SensorDeviceClass.ENUM,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ],
    },
    Capability.ROBOT_CLEANER_MOVEMENT: {
        Attribute.ROBOT_CLEANER_MOVEMENT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ROBOT_CLEANER_MOVEMENT,
                translation_key="robot_cleaner_movement",
                options=[
                    "homing",
                    "idle",
                    "charging",
                    "alarm",
                    "off",
                    "reserve",
                    "point",
                    "after",
                    "cleaning",
                    "pause",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: ROBOT_CLEANER_MOVEMENT_MAP.get(value, value),
            )
        ]
    },
    Capability.ROBOT_CLEANER_TURBO_MODE: {
        Attribute.ROBOT_CLEANER_TURBO_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ROBOT_CLEANER_TURBO_MODE,
                translation_key="robot_cleaner_turbo_mode",
                options=["on", "off", "silence", "extra_silence"],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: ROBOT_CLEANER_TURBO_MODE_STATE_MAP.get(
                    value, value
                ),
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.SIGNAL_STRENGTH: {
        Attribute.LQI: [
            SmartThingsSensorEntityDescription(
                key=Attribute.LQI,
                translation_key="link_quality",
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ],
        Attribute.RSSI: [
            SmartThingsSensorEntityDescription(
                key=Attribute.RSSI,
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ],
    },
    # Haven't seen at devices yet
    Capability.SMOKE_DETECTOR: {
        Attribute.SMOKE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.SMOKE,
                translation_key="smoke_detector",
                options=["detected", "clear", "tested"],
                device_class=SensorDeviceClass.ENUM,
            )
        ]
    },
    Capability.TEMPERATURE_MEASUREMENT: {
        Attribute.TEMPERATURE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.TEMPERATURE,
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.THERMOSTAT_COOLING_SETPOINT: {
        Attribute.COOLING_SETPOINT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.COOLING_SETPOINT,
                translation_key="thermostat_cooling_setpoint",
                device_class=SensorDeviceClass.TEMPERATURE,
                capability_ignore_list=[
                    {
                        Capability.AIR_CONDITIONER_FAN_MODE,
                        Capability.TEMPERATURE_MEASUREMENT,
                        Capability.AIR_CONDITIONER_MODE,
                    },
                    THERMOSTAT_CAPABILITIES,
                ],
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.THERMOSTAT_FAN_MODE: {
        Attribute.THERMOSTAT_FAN_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.THERMOSTAT_FAN_MODE,
                translation_key="thermostat_fan_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
                capability_ignore_list=[THERMOSTAT_CAPABILITIES],
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.THERMOSTAT_HEATING_SETPOINT: {
        Attribute.HEATING_SETPOINT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.HEATING_SETPOINT,
                translation_key="thermostat_heating_setpoint",
                device_class=SensorDeviceClass.TEMPERATURE,
                entity_category=EntityCategory.DIAGNOSTIC,
                capability_ignore_list=[THERMOSTAT_CAPABILITIES],
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.THERMOSTAT_MODE: {
        Attribute.THERMOSTAT_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.THERMOSTAT_MODE,
                translation_key="thermostat_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
                capability_ignore_list=[THERMOSTAT_CAPABILITIES],
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.THERMOSTAT_OPERATING_STATE: {
        Attribute.THERMOSTAT_OPERATING_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.THERMOSTAT_OPERATING_STATE,
                translation_key="thermostat_operating_state",
                capability_ignore_list=[THERMOSTAT_CAPABILITIES],
            )
        ]
    },
    # deprecated capability
    Capability.THERMOSTAT_SETPOINT: {
        Attribute.THERMOSTAT_SETPOINT: [
            SmartThingsSensorEntityDescription(
                key=Attribute.THERMOSTAT_SETPOINT,
                translation_key="thermostat_setpoint",
                device_class=SensorDeviceClass.TEMPERATURE,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    Capability.THREE_AXIS: {
        Attribute.THREE_AXIS: [
            SmartThingsSensorEntityDescription(
                key="x_coordinate",
                translation_key="x_coordinate",
                value_fn=lambda value: value[0],
            ),
            SmartThingsSensorEntityDescription(
                key="y_coordinate",
                translation_key="y_coordinate",
                value_fn=lambda value: value[1],
            ),
            SmartThingsSensorEntityDescription(
                key="z_coordinate",
                translation_key="z_coordinate",
                value_fn=lambda value: value[2],
            ),
        ]
    },
    Capability.TV_CHANNEL: {
        Attribute.TV_CHANNEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.TV_CHANNEL,
                translation_key="tv_channel",
            )
        ],
        Attribute.TV_CHANNEL_NAME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.TV_CHANNEL_NAME,
                translation_key="tv_channel_name",
            )
        ],
    },
    # Haven't seen at devices yet
    Capability.TVOC_MEASUREMENT: {
        Attribute.TVOC_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.TVOC_LEVEL,
                device_class=SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS_PARTS,
                native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # Haven't seen at devices yet
    Capability.ULTRAVIOLET_INDEX: {
        Attribute.ULTRAVIOLET_INDEX: [
            SmartThingsSensorEntityDescription(
                key=Attribute.ULTRAVIOLET_INDEX,
                translation_key="uv_index",
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.VERY_FINE_DUST_SENSOR: {
        Attribute.VERY_FINE_DUST_LEVEL: [
            SmartThingsSensorEntityDescription(
                key=Attribute.VERY_FINE_DUST_LEVEL,
                native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
                device_class=SensorDeviceClass.PM1,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    Capability.VOLTAGE_MEASUREMENT: {
        Attribute.VOLTAGE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.VOLTAGE,
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            )
        ]
    },
    # part of the proposed spec
    Capability.WASHER_MODE: {
        Attribute.WASHER_MODE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.WASHER_MODE,
                translation_key="washer_mode",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        ]
    },
    Capability.WASHER_OPERATING_STATE: {
        Attribute.MACHINE_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.MACHINE_STATE,
                translation_key="washer_machine_state",
                options=WASHER_OPTIONS,
                device_class=SensorDeviceClass.ENUM,
            )
        ],
        Attribute.WASHER_JOB_STATE: [
            SmartThingsSensorEntityDescription(
                key=Attribute.WASHER_JOB_STATE,
                translation_key="washer_job_state",
                options=[
                    "air_wash",
                    "ai_rinse",
                    "ai_spin",
                    "ai_wash",
                    "cooling",
                    "delay_wash",
                    "drying",
                    "finish",
                    "none",
                    "pre_wash",
                    "rinse",
                    "spin",
                    "wash",
                    "weight_sensing",
                    "wrinkle_prevent",
                    "freeze_protection",
                ],
                device_class=SensorDeviceClass.ENUM,
                value_fn=lambda value: JOB_STATE_MAP.get(value, value),
            )
        ],
        Attribute.COMPLETION_TIME: [
            SmartThingsSensorEntityDescription(
                key=Attribute.COMPLETION_TIME,
                translation_key="completion_time",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=dt_util.parse_datetime,
            )
        ],
    },
}


UNITS = {
    "C": UnitOfTemperature.CELSIUS,
    "F": UnitOfTemperature.FAHRENHEIT,
    "lux": LIGHT_LUX,
    "mG": None,
    "μg/m^3": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartThingsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for a config entry."""
    entry_data = entry.runtime_data
    async_add_entities(
        SmartThingsSensor(
            entry_data.client,
            device,
            description,
            capability,
            attribute,
        )
        for device in entry_data.devices.values()
        for capability, attributes in CAPABILITY_TO_SENSORS.items()
        if capability in device.status[MAIN]
        for attribute, descriptions in attributes.items()
        for description in descriptions
        if (
            not description.capability_ignore_list
            or not any(
                all(capability in device.status[MAIN] for capability in capability_list)
                for capability_list in description.capability_ignore_list
            )
        )
        and (
            not description.exists_fn
            or description.exists_fn(device.status[MAIN][capability][attribute])
        )
    )


class SmartThingsSensor(SmartThingsEntity, SensorEntity):
    """Define a SmartThings Sensor."""

    entity_description: SmartThingsSensorEntityDescription

    def __init__(
        self,
        client: SmartThings,
        device: FullDevice,
        entity_description: SmartThingsSensorEntityDescription,
        capability: Capability,
        attribute: Attribute,
    ) -> None:
        """Init the class."""
        capabilities_to_subscribe = {capability}
        if entity_description.use_temperature_unit:
            capabilities_to_subscribe.add(Capability.TEMPERATURE_MEASUREMENT)
        super().__init__(client, device, capabilities_to_subscribe)
        self._attr_unique_id = f"{device.device.device_id}_{MAIN}_{capability}_{attribute}_{entity_description.key}"
        self._attribute = attribute
        self.capability = capability
        self.entity_description = entity_description

    @property
    def native_value(self) -> str | float | datetime | int | None:
        """Return the state of the sensor."""
        res = self.get_attribute_value(self.capability, self._attribute)
        return self.entity_description.value_fn(res)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit this state is expressed in."""
        if self.entity_description.use_temperature_unit:
            unit = self._internal_state[Capability.TEMPERATURE_MEASUREMENT][
                Attribute.TEMPERATURE
            ].unit
        else:
            unit = self._internal_state[self.capability][self._attribute].unit
        return (
            UNITS.get(unit, unit)
            if unit
            else self.entity_description.native_unit_of_measurement
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes."""
        if self.entity_description.extra_state_attributes_fn:
            return self.entity_description.extra_state_attributes_fn(
                self.get_attribute_value(self.capability, self._attribute)
            )
        return None

    @property
    def options(self) -> list[str] | None:
        """Return the options for this sensor."""
        if self.entity_description.options_attribute:
            if (
                options := self.get_attribute_value(
                    self.capability, self.entity_description.options_attribute
                )
            ) is None:
                return []
            return [option.lower() for option in options]
        return super().options

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        await super().async_added_to_hass()
        if (
            not self.entity_description.deprecated
            or (reason := self.entity_description.deprecated(self.device.status[MAIN]))
            is None
        ):
            return
        automations = automations_with_entity(self.hass, self.entity_id)
        scripts = scripts_with_entity(self.hass, self.entity_id)
        if not automations and not scripts:
            return

        entity_reg: er.EntityRegistry = er.async_get(self.hass)
        items_list = [
            f"- [{item.original_name}](/config/{integration}/edit/{item.unique_id})"
            for integration, entities in (
                ("automation", automations),
                ("script", scripts),
            )
            for entity_id in entities
            if (item := entity_reg.async_get(entity_id))
        ]

        async_create_issue(
            self.hass,
            DOMAIN,
            f"deprecated_{reason}_{self.entity_id}",
            breaks_in_ha_version="2025.10.0",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=f"deprecated_{reason}",
            translation_placeholders={
                "entity": self.entity_id,
                "items": "\n".join(items_list),
            },
        )

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        if (
            not self.entity_description.deprecated
            or (reason := self.entity_description.deprecated(self.device.status[MAIN]))
            is None
        ):
            return
        async_delete_issue(self.hass, DOMAIN, f"deprecated_{reason}_{self.entity_id}")
