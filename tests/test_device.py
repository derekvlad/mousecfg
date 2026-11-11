"""Тесты HID++-устройства без реального железа.

Проверяем кадры, которые уходят в устройство: подсветка обязана сначала
забрать SW-контроль (RGB_EFFECTS 0x50), иначе прошивка игнорирует эффект.
"""

from __future__ import annotations

from mousecfg.hidpp import protocol
from mousecfg.hidpp.device import (
    SW_CONTROL_CLAIM,
    HidppDevice,
    _lighting_effect_bytes,
)


class RecordingDevice(HidppDevice):
    """Устройство без транспорта: пишем только пришедшие запросы."""

    def __init__(self) -> None:  # noqa: D107
        self.calls: list[tuple[int, int, bytes]] = []

    def request(self, feature, function, *params, **kwargs):  # noqa: ANN001, D102
        payload = b"".join(
            bytes((value,)) if isinstance(value, int) else bytes(value) for value in params
        )
        self.calls.append((feature, function, payload))
        return b""

    def lighting_zone(self, index: int = 0):  # noqa: ANN201, D102
        raise AssertionError("в тесте слот задаётся явно")


def test_set_lighting_claims_sw_control_first():
    device = RecordingDevice()
    device.set_lighting(0x01, {"color": 0xFF0000}, zone=0, slot=1)

    assert [function for _feature, function, _payload in device.calls] == [0x50, 0x10]
    feature, function, payload = device.calls[0]
    assert feature == protocol.FeatureId.RGB_EFFECTS
    assert (function, payload) == (0x50, SW_CONTROL_CLAIM)


def test_set_lighting_effect_payload_shape():
    device = RecordingDevice()
    device.set_lighting(0x01, {"color": 0xFF0000}, zone=0, slot=1)

    _feature, _function, payload = device.calls[1]
    # Префикс — зона, далее 11 байт эффекта (слот + 10 параметров), суффикс 0x01.
    assert payload == bytes([0x00, 0x01, 0xFF, 0x00, 0x00]) + bytes(7) + bytes([0x01])


def test_lighting_effect_bytes_places_color_like_solaar():
    body = _lighting_effect_bytes(0x01, 0x01, {"color": 0x57E389})
    assert body == bytes([0x01, 0x57, 0xE3, 0x89]) + bytes(7)
