"""Devices: ESP32 boards and Tuya modules with their state, outputs and inputs; naming them, testing an output, and
importing Tuya modules from the Tuya developer account.

The recognition service holds the connections (devices/manager.py): a test is a row in device_commands that it
carries out. Everyone signed in sees the devices; only administrators change them or test an output, since an
output can drive a lock.
"""

from __future__ import annotations

import ipaddress
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from live_tracker.db import (
    DEVICES_VERSION, Device, DeviceCommand, DeviceOutput, audit, bump, get_setting, set_setting,
)
from live_tracker.devices import BAD_KEY, BROKER, NO_ANSWER, NO_OUTPUT, NO_TIMER, NOT_FOUND, OFFLINE, REFUSED
from live_tracker.devices.tuya import MAX_MS as TUYA_MAX_MS
from live_tracker.devices.tuya import CloudDevice, TuyaCloudError, cloud_devices
from live_tracker.users import is_admin
from live_tracker.web.common import flash, fragment, page, require_admin, require_user, services, translator

router = APIRouter(dependencies=[Depends(require_user)])

DEVICE_KINDS = {"esp32": "ESP32 board", "tuya": "Tuya module"}
OUTPUT_KINDS = {"lock": "Door lock", "light": "Light", "siren": "Siren", "relay": "Other"}
INPUT_KINDS = {"button": "Button", "contact": "Door contact"}
INPUT_VALUES = {"pressed": "Pressed", "open": "Door open", "closed": "Door closed"}
# Why a device isn't online, as its card says it (devices/__init__.py).
DEVICE_PROBLEMS = {
    NOT_FOUND: "Not found on the network. If it is on another network, type its address in its settings.",
    NO_ANSWER: "Not answering. Check that it has power and Wi-Fi.",
    BAD_KEY: "It refuses its key: it was probably paired again. Import the Tuya devices again.",
    BROKER: "The MQTT broker isn't connected, so the ESP32 boards can't be reached.",
}
# Why a test didn't switch the output on.
COMMAND_ERRORS = {
    OFFLINE: "The device is offline.",
    NO_ANSWER: "The device didn't confirm.",
    NO_TIMER: "This output can't switch itself off, so it is never switched on.",
    NO_OUTPUT: "The device doesn't have this output any more.",
    REFUSED: "The device refused the command.",
    BROKER: "The MQTT broker isn't connected, so the ESP32 boards can't be reached.",
    "late": "The recognition service didn't take the command in time, so it was dropped.",
}
POWER_ON = {"on": "After a power cut, its relays switch on. Set them to stay off in Smart Life.",
            "memory": "After a power cut, its relays go back to how they were. Set them to stay off in Smart Life."}
CLOUD_ERRORS = {
    "keys": "Paste the Access ID and the Secret from the project's Overview.",
    "login": "Tuya refused the Access ID or the Secret. Copy them again from the project's Overview.",
    "list": "Tuya didn't list the devices. Check the data center, and that Smart Life is linked to the project.",
    "network": "Tuya couldn't be reached. Check the internet connection.",
}
TUYA_REGIONS = {"us": "Western America", "ue": "Eastern America", "eu": "Central Europe", "we": "Western Europe",
                "in": "India", "sg": "Singapore", "cn": "China"}  # Tuya's data centers (devices/tuya.py REGIONS)
TUYA_ACCESS_ID, TUYA_SECRET, TUYA_REGION = "tuya_access_id", "tuya_secret", "tuya_region"
TEST_MS = 1000  # a test is a short pulse
TEST_WAIT_S = 8.0  # how long a test waits for the recognition service and the device


@router.get("/devices", response_class=HTMLResponse)
def device_list(request: Request) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        devices = _all(session)
        admin = is_admin(session, request.session["user"])
    return page(request, "devices.html", section="devices", devices=devices, is_admin=admin,
                mqtt_host=s.cfg.mqtt.host, **_live(request), **_labels())


@router.get("/devices/states", response_class=HTMLResponse)
def device_states(request: Request) -> HTMLResponse:
    """Every device's state, swapped into the open page every few seconds."""
    with services(request).db.session() as session:
        devices = _all(session)
    return fragment(request, "_device_states.html", devices=devices, **_live(request), **_labels())


@router.get("/devices/tuya", response_class=HTMLResponse)
def tuya_page(request: Request, user: str = Depends(require_admin)) -> HTMLResponse:
    return _tuya_page(request, found=None, error="")


@router.post("/devices/tuya", response_class=HTMLResponse)
def tuya_list(request: Request, access_id: str = Form(""), secret: str = Form(""), region: str = Form("us"),
              user: str = Depends(require_admin)) -> HTMLResponse:
    """Saves the developer project's keys, then lists the devices of the Smart Life account linked to it."""
    with services(request).db.session() as session:
        set_setting(session, TUYA_ACCESS_ID, access_id.strip())
        if secret.strip():  # empty keeps the saved one, which the page never shows
            set_setting(session, TUYA_SECRET, secret.strip())
        set_setting(session, TUYA_REGION, region if region in TUYA_REGIONS else "us")
        audit(session, user, "Tuya account changed")
    found, error = _cloud(request)
    return _tuya_page(request, found=found, error=error, status_code=400 if error else 200)


@router.post("/devices/tuya/import", response_model=None)
def tuya_import(request: Request, uids: list[str] = Form([]),
                user: str = Depends(require_admin)) -> HTMLResponse | RedirectResponse:
    """Adds the devices chosen, or gives those already here their current key. The keys come from Tuya again here:
    they never pass through the browser."""
    found, error = _cloud(request)
    if error:
        return _tuya_page(request, found=None, error=error, status_code=400)
    chosen = [d for d in found if d.uid in set(uids) and d.outputs]
    if not chosen:
        return _tuya_page(request, found=found, error="Choose at least one device with a switch.", status_code=400)
    t = translator(request)
    with services(request).db.session() as session:
        for cloud_device in chosen:
            _import(session, cloud_device, t)
        bump(session, DEVICES_VERSION)
        audit(session, user, "Tuya devices imported", ", ".join(d.name for d in chosen))
    flash(request, "Tuya devices added or updated: {count}.", count=len(chosen))
    return RedirectResponse("/devices", status_code=303)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_edit(request: Request, device_id: int, user: str = Depends(require_admin)) -> HTMLResponse:
    with services(request).db.session() as session:
        device = _get(session, device_id)
        form = _form_of(device)
    return page(request, "device_form.html", section="devices", device=device, form=form, error="",
                **_live(request), **_labels())


@router.post("/devices/{device_id}", response_model=None)
def device_update(
    request: Request, device_id: int, name: str = Form(""), address: str = Form(""),
    output_id: list[int] = Form([]), output_name: list[str] = Form([]), output_kind: list[str] = Form([]),
    output_seconds: list[str] = Form([]), input_id: list[int] = Form([]), input_name: list[str] = Form([]),
    user: str = Depends(require_admin),
) -> HTMLResponse | RedirectResponse:
    outputs = list(zip(output_id, output_name, output_kind, output_seconds))
    inputs = list(zip(input_id, input_name))
    with services(request).db.session() as session:
        device = _get(session, device_id)
        error = _device_error(device, name, address, outputs, inputs)
        if error:
            form = {"name": name, "address": address,
                    "outputs": {i: {"name": n, "kind": k, "seconds": sec} for i, n, k, sec in outputs},
                    "inputs": {i: {"name": n} for i, n in inputs}}
            return page(request, "device_form.html", section="devices", device=device, form=form, error=error,
                        status_code=400, **_live(request), **_labels())
        device.name, device.new = name.strip(), False
        if device.kind == "tuya":
            device.address = address.strip()
        by_id = {o.id: o for o in device.outputs}
        for output_key, output_name_, kind, seconds in outputs:
            output = by_id[output_key]
            output.name, output.kind = output_name_.strip(), kind
            output.pulse_ms = round(float(seconds.replace(",", ".")) * 1000)
        inputs_by_id = {i.id: i for i in device.inputs}
        for input_key, input_name_ in inputs:
            inputs_by_id[input_key].name = input_name_.strip()
        bump(session, DEVICES_VERSION)
        audit(session, user, "device changed", device.name)
    flash(request, "Changes saved.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{device_id}/delete")
def device_delete(request: Request, device_id: int, confirm: bool = Form(False),
                  user: str = Depends(require_admin)) -> RedirectResponse:
    if not confirm:
        flash(request, "Confirm that you want to remove the device.", "bad")
        return RedirectResponse(f"/devices/{device_id}", status_code=303)
    with services(request).db.session() as session:
        device = _get(session, device_id)
        name = device.name
        session.delete(device)
        bump(session, DEVICES_VERSION)
        audit(session, user, "device removed", name)
    flash(request, "{name} removed.", name=name)
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{device_id}/outputs/{output_id}/test", response_class=HTMLResponse)
def output_test(request: Request, device_id: int, output_id: int, user: str = Depends(require_admin)) -> HTMLResponse:
    """A short pulse, carried out by the recognition service. Waits for the device's answer."""
    s = services(request)
    with s.db.session() as session:
        output = session.get(DeviceOutput, output_id)
        if output is None or output.device_id != device_id:
            raise HTTPException(404)
        name = output.name
        if s.vision.status() is None:
            return fragment(request, "_device_test.html", name=name, result="failed",
                            error="The recognition service is not running, so devices can't be reached.")
        command = DeviceCommand(device_id=device_id, output_key=output.key, ms=TEST_MS, requested_by=user)
        session.add(command)
        session.flush()
        command_id = command.id
        audit(session, user, "output tested", f"{output.device.name}: {name}")
    result, code = _wait_for(s, command_id)
    if result == "pending":
        error = "The recognition service didn't answer. Check that it is running."
    else:
        error = COMMAND_ERRORS.get(code, "")
    return fragment(request, "_device_test.html", name=name, result=result, error=error)


def _wait_for(s, command_id: int) -> tuple[str, str]:
    """The command's result and error once the recognition service is done with it; "pending" if it never is."""
    deadline = time.monotonic() + TEST_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(0.1)
        with s.db.session() as session:
            command = session.get(DeviceCommand, command_id)
            if command is None:
                break
            if command.result in ("ok", "failed", "late"):
                return command.result, command.error if command.result == "failed" else command.result
    return "pending", ""


def _all(session: Session) -> list[Device]:
    """New boards first, to be named, then by name."""
    options = (selectinload(Device.outputs), selectinload(Device.inputs))
    return list(session.scalars(select(Device).options(*options).order_by(Device.new.desc(), Device.name)))


def _get(session: Session, device_id: int) -> Device:
    device = session.get(Device, device_id, options=(selectinload(Device.outputs), selectinload(Device.inputs)))
    if device is None:
        raise HTTPException(404)
    return device


def _live(request: Request) -> dict:
    """The devices' state in the recognition service, and whether it runs."""
    status = services(request).vision.status()
    return {"service_up": status is not None, "states": (status or {}).get("devices", {}),
            "broker": (status or {}).get("broker", "off")}


def _labels() -> dict:
    return {"device_kinds": DEVICE_KINDS, "output_kinds": OUTPUT_KINDS, "input_kinds": INPUT_KINDS,
            "input_values": INPUT_VALUES, "problems": DEVICE_PROBLEMS}


def _form_of(device: Device) -> dict:
    return {"name": device.name, "address": device.address,
            "outputs": {o.id: {"name": o.name, "kind": o.kind, "seconds": _seconds(o.pulse_ms)} for o in device.outputs},
            "inputs": {i.id: {"name": i.name} for i in device.inputs}}


def _seconds(ms: int) -> str:
    return str(ms // 1000) if ms % 1000 == 0 else str(ms / 1000)


def _device_error(device: Device, name: str, address: str, outputs: list, inputs: list) -> str:
    if not name.strip():
        return "Give the device a name, such as Staff entrance door."
    if device.kind == "tuya" and address.strip():
        try:
            ipaddress.IPv4Address(address.strip())
        except ValueError:
            return "Write the address as four numbers, such as 192.168.100.102, or leave it empty."
    by_id = {o.id: o for o in device.outputs}
    for output_key, output_name, kind, seconds in outputs:
        output = by_id.get(output_key)
        if output is None:
            return "This device has changed. Open its settings again."
        if not output_name.strip():
            return "Give every output a name, such as Door lock."
        if kind not in OUTPUT_KINDS:
            return "Choose what each output switches."
        try:
            ms = float(seconds.replace(",", ".")) * 1000
        except ValueError:
            ms = 0
        if not (1000 <= ms <= output.max_ms and ms % 1000 == 0):  # Tuya modules count whole seconds
            return "Choose how long each output stays on: whole seconds, from 1 up to its longest time."
    if any(i not in {x.id for x in device.inputs} for i, _ in inputs):
        return "This device has changed. Open its settings again."
    if any(not n.strip() for _, n in inputs):
        return "Give every input a name, such as Exit button."
    return ""


def _tuya_page(request: Request, found: list[CloudDevice] | None, error: str, status_code: int = 200) -> HTMLResponse:
    with services(request).db.session() as session:
        access_id = get_setting(session, TUYA_ACCESS_ID) or ""
        has_secret = bool(get_setting(session, TUYA_SECRET))
        region = get_setting(session, TUYA_REGION) or "us"
        known = {d.uid: d.name for d in session.scalars(select(Device).where(Device.kind == "tuya"))}
    return page(request, "devices_tuya.html", section="devices", access_id=access_id, has_secret=has_secret,
                region=region, regions=TUYA_REGIONS, found=found, known=known, error=error, power_on=POWER_ON,
                status_code=status_code)


def _cloud(request: Request) -> tuple[list[CloudDevice], str]:
    """The Tuya account's devices, or why they couldn't be listed (a key of CLOUD_ERRORS' texts)."""
    with services(request).db.session() as session:
        access_id = get_setting(session, TUYA_ACCESS_ID) or ""
        secret = get_setting(session, TUYA_SECRET) or ""
        region = get_setting(session, TUYA_REGION) or "us"
    if not access_id or not secret:
        return [], CLOUD_ERRORS["keys"]
    try:
        return cloud_devices(access_id, secret, region), ""
    except TuyaCloudError as e:
        return [], CLOUD_ERRORS.get(e.code, CLOUD_ERRORS["list"])


def _import(session: Session, found: CloudDevice, t) -> None:
    """A Tuya module, added with a relay output per channel, or one already here with its new key."""
    device = session.scalar(select(Device).where(Device.uid == found.uid))
    if device is None:
        device = Device(kind="tuya", uid=found.uid, name=(found.name or found.uid)[:80])
        session.add(device)
    device.local_key, device.model = found.local_key, found.model[:80]
    known = {o.key: o for o in device.outputs}
    for output in found.outputs:
        if output.key in known:
            known[output.key].timer_dp = output.timer_dp
        else:
            device.outputs.append(DeviceOutput(key=output.key, name=t("Channel {number}", number=output.number),
                                               kind="relay", pulse_ms=3000, max_ms=TUYA_MAX_MS,
                                               timer_dp=output.timer_dp))
