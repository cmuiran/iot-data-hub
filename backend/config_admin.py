"""Change the configuration through the API, safely, and apply it without a restart.

Every change is applied to a temporary copy of the whole config directory first and validated
with the same loader the hub uses (cross-device checks included: duplicate ids, Modbus address
clashes, topic collisions, unknown tags in alarm/control rules). Only a change that adds no
ERROR is written — atomically (temp file + rename) — and then applied with ``Hub.reload``. Each
change leaves a ``config_changed`` row in auth_audit with the user, the action and the result.

Files written by the API are regenerated from the models, so hand-written comments in those
files are not kept. In a two-node (HA) setup both nodes must read the same config directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from backend.errors import Conflict, Invalid, NotFound
from backend.rbac import Principal
from hub.config_loader import (
    ALARMS_FILE,
    CONTROL_FILE,
    DEVICES_DIR,
    ConfigReport,
    Severity,
    load_config,
)
from hub.drivers.base import utcnow
from hub.models import DEVICE_ADAPTER, AlarmRule, AlarmsFile, ControlFile, ControlPolicy

Mutation = Callable[[Path], None]


def _dump(model: Any) -> str:
    data = model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _issues(report: ConfigReport, only_errors: bool = True) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in report.issues
            if not only_errors or i.severity is Severity.ERROR]


def _model_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e["loc"]), "msg": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors(include_url=False)]


class ConfigAdmin:
    def __init__(self, services: Any) -> None:
        self.services = services
        self._lock = asyncio.Lock()

    @property
    def config_dir(self) -> Path:
        return Path(self.services.settings.config_dir)

    # --- generic machinery ----------------------------------------------------------------------

    def _trial(self, mutate: Mutation) -> ConfigReport:
        """Apply ``mutate`` to a scratch copy and validate the result."""
        with tempfile.TemporaryDirectory(prefix="hub-config-") as tmp:
            scratch = Path(tmp) / "config"
            shutil.copytree(self.config_dir, scratch,
                            ignore=shutil.ignore_patterns(".trash", "*.tmp"))
            mutate(scratch)
            return load_config(scratch)

    async def _apply(self, principal: Principal, action: str, target: str, mutate: Mutation,
                     detail: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            before = load_config(self.config_dir)
            trial = await asyncio.to_thread(self._trial, mutate)
            new_errors = [i for i in _issues(trial)
                          if i not in _issues(before)]  # pre-existing problems do not block
            if new_errors or trial.has_errors and not before.has_errors:
                raise Invalid("the change would make the configuration invalid",
                              code="config_invalid", extra={"issues": new_errors or
                                                            _issues(trial)})
            await asyncio.to_thread(mutate, self.config_dir)
            report = load_config(self.config_dir)
            changes = await self.services.reload(report)
            await self.services.store.add_auth_audit(
                "config_changed", principal.username, user_id=principal.user_id,
                detail={"action": action, "target": target, **(detail or {}),
                        "applied": {k: v for k, v in changes.items() if k != "unchanged"}})
            warnings = [i.model_dump(mode="json") for i in report.issues
                        if i.severity is Severity.WARNING]
            return {"action": action, "target": target, "changes": changes,
                    "warnings": warnings}

    async def reload_from_disk(self, principal: Principal) -> dict[str, Any]:
        async with self._lock:
            report = load_config(self.config_dir)
            if report.has_errors:
                raise Invalid("configuration on disk has errors", code="config_invalid",
                              extra={"issues": _issues(report)})
            changes = await self.services.reload(report)
            await self.services.store.add_auth_audit(
                "config_changed", principal.username, user_id=principal.user_id,
                detail={"action": "reload", "applied": {k: v for k, v in changes.items()
                                                        if k != "unchanged"}})
            return {"action": "reload", "changes": changes}

    def validate_device(self, data: dict[str, Any]) -> dict[str, Any]:
        """Dry run for a device body: schema errors plus cross-file issues it would cause."""
        try:
            device = DEVICE_ADAPTER.validate_python(data)
        except ValidationError as exc:
            return {"valid": False, "errors": _model_errors(exc), "issues": []}
        path = f"{DEVICES_DIR}/{device.device_id}.yaml"

        def mutate(root: Path) -> None:
            _write_atomic(root / path, _dump(device))

        report = self._trial(mutate)
        issues = [i for i in _issues(report, only_errors=False) if i["source"] == path]
        return {"valid": not any(i["severity"] == "ERROR" for i in issues), "errors": [],
                "issues": issues}

    # --- devices --------------------------------------------------------------------------------

    def _device_file(self, root: Path, device_id: str) -> Path:
        return root / DEVICES_DIR / f"{device_id}.yaml"

    def device_data(self, device_id: str) -> dict[str, Any]:
        device = self.services.hub.devices.get(device_id)
        if device is None:
            raise NotFound("no such device")
        return device.model_dump(mode="json", exclude_none=True)

    def _parse(self, data: dict[str, Any]) -> Any:
        try:
            return DEVICE_ADAPTER.validate_python(data)
        except ValidationError as exc:
            raise Invalid("device is not valid", code="device_invalid",
                          extra={"errors": _model_errors(exc)}) from exc

    async def create_device(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        device = self._parse(data)
        if self._device_file(self.config_dir, device.device_id).exists():
            raise Conflict(f"device {device.device_id} already exists", code="device_exists")
        return await self._apply(principal, "device_created", device.device_id,
                                 lambda root: _write_atomic(
                                     self._device_file(root, device.device_id), _dump(device)))

    async def replace_device(self, principal: Principal, device_id: str,
                             data: dict[str, Any]) -> dict[str, Any]:
        device = self._parse(data)
        if device.device_id != device_id:
            raise Invalid("device_id in the body must match the URL", code="id_mismatch")
        if not self._device_file(self.config_dir, device_id).exists():
            raise NotFound("no such device file")
        return await self._apply(principal, "device_updated", device_id,
                                 lambda root: _write_atomic(self._device_file(root, device_id),
                                                            _dump(device)))

    async def set_enabled(self, principal: Principal, device_id: str, enabled: bool
                          ) -> dict[str, Any]:
        current = self.device_data(device_id)
        return await self.replace_device(principal, device_id, current | {"enabled": enabled})

    async def delete_device(self, principal: Principal, device_id: str) -> dict[str, Any]:
        if not self._device_file(self.config_dir, device_id).exists():
            raise NotFound("no such device file")
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")

        def mutate(root: Path) -> None:
            src = self._device_file(root, device_id)
            trash = root / ".trash"
            trash.mkdir(exist_ok=True)
            shutil.move(src, trash / f"{device_id}-{stamp}.yaml")

        return await self._apply(principal, "device_deleted", device_id, mutate,
                                 {"kept_in": f".trash/{device_id}-{stamp}.yaml"})

    # --- alarms / control policy ------------------------------------------------------------------

    def _alarms(self, root: Path) -> AlarmsFile:
        path = root / ALARMS_FILE
        if not path.exists():
            return AlarmsFile()
        return AlarmsFile.model_validate(yaml.safe_load(path.read_text("utf-8")) or {})

    def _control(self, root: Path) -> ControlFile:
        path = root / CONTROL_FILE
        if not path.exists():
            return ControlFile()
        return ControlFile.model_validate(yaml.safe_load(path.read_text("utf-8")) or {})

    async def put_alarm_rule(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        try:
            rule = AlarmRule.model_validate(data)
        except ValidationError as exc:
            raise Invalid("alarm rule is not valid", code="rule_invalid",
                          extra={"errors": _model_errors(exc)}) from exc

        def mutate(root: Path) -> None:
            current = self._alarms(root)
            rules = [r for r in current.rules if r.tag != rule.tag] + [rule]
            _write_atomic(root / ALARMS_FILE,
                          _dump(current.model_copy(update={"rules": rules})))

        return await self._apply(principal, "alarm_rule_set", rule.tag, mutate,
                                 {"rule": rule.model_dump(mode="json", exclude_none=True)})

    async def delete_alarm_rule(self, principal: Principal, tag_path: str) -> dict[str, Any]:
        if not any(r.tag == tag_path for r in self._alarms(self.config_dir).rules):
            raise NotFound("no alarm rule for this tag")

        def mutate(root: Path) -> None:
            current = self._alarms(root)
            _write_atomic(root / ALARMS_FILE, _dump(current.model_copy(
                update={"rules": [r for r in current.rules if r.tag != tag_path]})))

        return await self._apply(principal, "alarm_rule_deleted", tag_path, mutate)

    async def put_control_policy(self, principal: Principal, tag_path: str | None,
                                 data: dict[str, Any]) -> dict[str, Any]:
        """``tag_path=None`` replaces the defaults."""
        try:
            policy = ControlPolicy.model_validate(data)
        except ValidationError as exc:
            raise Invalid("control policy is not valid", code="policy_invalid",
                          extra={"errors": _model_errors(exc)}) from exc

        def mutate(root: Path) -> None:
            current = self._control(root)
            if tag_path is None:
                updated = current.model_copy(update={"defaults": policy})
            else:
                updated = current.model_copy(update={"tags": current.tags | {tag_path: policy}})
            _write_atomic(root / CONTROL_FILE, _dump(updated))

        return await self._apply(principal, "control_policy_set", tag_path or "defaults",
                                 mutate, {"policy": policy.model_dump(mode="json",
                                                                      exclude_none=True)})

    async def delete_control_policy(self, principal: Principal, tag_path: str
                                    ) -> dict[str, Any]:
        if tag_path not in self._control(self.config_dir).tags:
            raise NotFound("no control policy for this tag")

        def mutate(root: Path) -> None:
            current = self._control(root)
            tags = {k: v for k, v in current.tags.items() if k != tag_path}
            _write_atomic(root / CONTROL_FILE, _dump(current.model_copy(update={"tags": tags})))

        return await self._apply(principal, "control_policy_deleted", tag_path, mutate)
