from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from hub.config_loader import Severity
from hub.models import AlarmRule, ControlFile, ControlPolicy
from tests.hub.builders import WriteConfig, load, messages, modbus_device


def _meter() -> dict[str, Any]:
    device = modbus_device()
    device["tags"] += [
        {"name": "limit", "data_type": "uint16", "register_type": "holding", "address": 10,
         "access": "read_write", "min": 0, "max": 500},
        {"name": "breaker", "data_type": "bool", "register_type": "discrete_input", "address": 0},
    ]
    return device


def _config(write_config: WriteConfig, alarms: Any = None, control: Any = None) -> Any:
    path = write_config(_meter())
    if alarms is not None:
        (path / "alarms.yaml").write_text(yaml.safe_dump(alarms), encoding="utf-8")
    if control is not None:
        (path / "control.yaml").write_text(yaml.safe_dump(control), encoding="utf-8")
    return load(path)


V = "kish.substation.meter_01.voltage"
LIMIT = "kish.substation.meter_01.limit"
BREAKER = "kish.substation.meter_01.breaker"


# --- models ---------------------------------------------------------------------------------


def test_alarm_rule_needs_a_condition() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        AlarmRule.model_validate({"tag": V})


def test_alarm_limits_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        AlarmRule.model_validate({"tag": V, "lo": 10, "hi": 5})
    AlarmRule.model_validate({"tag": V, "lolo": 1, "lo": 2, "hi": 3, "hihi": 4})


def test_alarm_equals_excludes_limits() -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        AlarmRule.model_validate({"tag": BREAKER, "equals": False, "hi": 1})


def test_control_policy_merges_with_defaults() -> None:
    control = ControlFile.model_validate({
        "defaults": {"min_interval_s": 5, "readback_timeout_s": 9},
        "tags": {LIMIT: {"confirm": True}},
    })
    policy = control.policy_for(LIMIT)
    assert policy.confirm is True and policy.min_interval_s == 5 and policy.readback_timeout_s == 9
    assert control.policy_for(V) == control.defaults
    assert ControlPolicy().confirm is False  # default: no second person


# --- cross checks ---------------------------------------------------------------------------


def test_valid_alarm_and_control_config(write_config: WriteConfig) -> None:
    report = _config(
        write_config,
        alarms={"rules": [{"tag": V, "hi": 250}, {"tag": BREAKER, "equals": False}]},
        control={"tags": {LIMIT: {"confirm": True, "max_step": 50, "interlocks": [
            {"tag": BREAKER, "op": "eq", "value": True, "message": "breaker closed"}]}}},
    )
    assert messages(report, Severity.ERROR) == []
    assert len(report.alarms.rules) == 2
    assert report.control.policy_for(LIMIT).max_step == 50


@pytest.mark.parametrize(
    "rule,error",
    [({"tag": "kish.substation.meter_01.nope", "hi": 1}, "unknown tag"),
     ({"tag": BREAKER, "hi": 1}, "need a numeric tag"),
     ({"tag": V, "equals": True}, "equals is for bool tags")],
)
def test_alarm_rule_errors(write_config: WriteConfig, rule: dict, error: str) -> None:
    report = _config(write_config, alarms={"rules": [rule]})
    assert any(error in m for m in messages(report, Severity.ERROR)), messages(report)


def test_duplicate_alarm_rule(write_config: WriteConfig) -> None:
    report = _config(write_config, alarms={"rules": [{"tag": V, "hi": 1}, {"tag": V, "lo": 0}]})
    assert any("only one alarm rule" in m for m in messages(report, Severity.ERROR))


@pytest.mark.parametrize(
    "control,error",
    [({"tags": {V: {"confirm": True}}}, "is not read_write"),
     ({"tags": {"kish.substation.meter_01.nope": {}}}, "unknown tag"),
     ({"tags": {LIMIT: {"interlocks": [{"tag": BREAKER, "op": "eq", "value": 1,
                                         "message": "m"}]}}}, "value must be true or false"),
     ({"tags": {LIMIT: {"interlocks": [{"tag": V, "op": "gt", "value": "x",
                                         "message": "m"}]}}}, "value must be a number"),
     ({"defaults": {"interlocks": [{"tag": "kish.x.y.z", "op": "eq", "value": True,
                                    "message": "m"}]}}, "unknown interlock tag")],
)
def test_control_errors(write_config: WriteConfig, control: dict, error: str) -> None:
    report = _config(write_config, control=control)
    assert any(error in m for m in messages(report, Severity.ERROR)), messages(report)


def test_schema_errors_in_alarms_file(write_config: WriteConfig) -> None:
    report = _config(write_config, alarms={"rules": [{"tag": V, "hi": 1, "colour": "red"}]})
    assert any("alarms.yaml" in m and "Extra inputs" in m for m in messages(report))


# --- notifications.yaml -----------------------------------------------------------------------


def test_notifications_config_checks(write_config: WriteConfig) -> None:
    path = write_config(_meter())
    (path / "notifications.yaml").write_text(yaml.safe_dump({
        "channels": {"mail": {"type": "email", "host": "smtp.kish.local", "sender": "hub@x",
                              "recipients": ["a@x"], "username_env": "HUB_SMTP_USER",
                              "password_env": "HUB_SMTP_PASSWORD"}},
        "rules": [{"name": "crit", "channels": ["mail", "sms"]},
                  {"name": "crit", "channels": ["mail"]}]}), encoding="utf-8")
    report = load(path)
    errors = messages(report, Severity.ERROR)
    assert any("unknown channel 'sms'" in e for e in errors)
    assert any("duplicate rule name 'crit'" in e for e in errors)
    assert any("HUB_SMTP_PASSWORD is not set" in w for w in messages(report, Severity.WARNING))


def test_notifications_schema(write_config: WriteConfig) -> None:
    path = write_config(_meter())
    (path / "notifications.yaml").write_text(yaml.safe_dump({"channels": {"x": {
        "type": "webhook", "url": "ftp://nope"}}}), encoding="utf-8")
    assert any("not an http(s) URL" in m for m in messages(load(path), Severity.ERROR))
