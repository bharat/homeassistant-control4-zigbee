"""Tests for frontend registration helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.control4_dimmers.frontend import JSModuleRegistration


class TestJSModuleRegistration:
    """Tests for utility methods on JSModuleRegistration."""

    def test_get_path_strips_query(self) -> None:
        reg = JSModuleRegistration.__new__(JSModuleRegistration)
        url = "/control4_dimmers/card.js?v=1.0"
        assert reg._get_path(url) == "/control4_dimmers/card.js"
        assert reg._get_path("/control4_dimmers/card.js") == "/control4_dimmers/card.js"

    def test_get_version_extracts_version(self) -> None:
        reg = JSModuleRegistration.__new__(JSModuleRegistration)
        assert reg._get_version("/card.js?v=2.3.1") == "2.3.1"
        assert reg._get_version("/card.js") == "0"
        assert reg._get_version("/card.js?other=1") == "0"

    def test_supports_lovelace_resources_false_when_no_lovelace(self) -> None:
        reg = JSModuleRegistration.__new__(JSModuleRegistration)
        reg.lovelace = None
        assert reg._supports_lovelace_resources() is False

    def test_supports_lovelace_resources_false_when_no_resources_attr(self) -> None:
        reg = JSModuleRegistration.__new__(JSModuleRegistration)

        class FakeLovelace:
            pass

        reg.lovelace = FakeLovelace()
        assert reg._supports_lovelace_resources() is False


# ── card HTML escaping ───────────────────────────────────────────────

CARD_JS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "control4_dimmers"
    / "frontend"
    / "www"
    / "control4-dimmer-card.js"
)

# Values the card interpolates into innerHTML that do not originate in
# the card file itself: (raw form that must be gone, escaped form that
# must be present).
UNTRUSTED_INTERPOLATIONS = [
    ("${dev.friendly_name}", "${esc(dev.friendly_name)}"),
    (
        "${cfg.name || `Button ${btn.startSlot}`}",
        "${esc(cfg.name || `Button ${btn.startSlot}`)}",
    ),
    ('value="${slot.name || ""}"', 'value="${esc(slot.name || "")}"'),
    (
        'placeholder="Button ${slot.slot_id}"',
        'placeholder="Button ${esc(slot.slot_id)}"',
    ),
    ('value="${e.entity_id}"', 'value="${esc(e.entity_id)}"'),
    ("${e.display_name}", "${esc(e.display_name)}"),
    ('title="${e.name}"', 'title="${esc(e.name)}"'),
    ('${selected?.name || ""}', '${esc(selected?.name || "")}'),
    ("${entityName}", "${esc(entityName)}"),
    ('icon="${entityIcon}"', 'icon="${esc(entityIcon)}"'),
    ('icon="${svcIcon}"', 'icon="${esc(svcIcon)}"'),
    ("${domainLabel} '${svcLabel}'", "${esc(domainLabel)} '${esc(svcLabel)}'"),
    (
        '<div class="save-error">${this._saveError}</div>',
        '<div class="save-error">${esc(this._saveError)}</div>',
    ),
    (
        '<div class="grid-title">${title}</div>',
        '<div class="grid-title">${esc(title)}</div>',
    ),
    ('value="${this._config.title || ""}"', 'value="${esc(this._config.title || "")}"'),
    (
        'value="${this._config.columns || 4}"',
        'value="${esc(this._config.columns || 4)}"',
    ),
]

XSS_PAYLOAD = "<img src=x onerror=alert(1)>"


class TestCardEscaping:
    """The Lovelace card never puts untrusted data into innerHTML raw."""

    def test_untrusted_interpolations_are_escaped(self) -> None:
        src = CARD_JS.read_text()
        assert "function esc(" in src
        for raw, escaped in UNTRUSTED_INTERPOLATIONS:
            assert raw not in src, f"raw interpolation: {raw}"
            assert escaped in src, f"no longer escaped: {escaped}"

    def test_colors_are_validated_not_escaped(self) -> None:
        src = CARD_JS.read_text()
        assert "function safeHex(" in src
        # The two innerHTML LED dots (dashboard chassis and editor chassis)
        # must take their color from safeHex(). Direct CSSOM assignments
        # elsewhere are not HTML and are not counted here.
        assert src.count("background:#${color}; ${ledRingStyle(color)}") == 2
        assert src.count("const color = safeHex(ledColor(") == 2

    def test_escape_helper_renders_payload_as_text(self) -> None:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        src = CARD_JS.read_text()
        helper = re.search(
            r"^const HTML_ESCAPES = .*?^}\n", src, re.DOTALL | re.MULTILINE
        )
        assert helper is not None
        script = (
            helper.group(0)
            + f"const out = esc({XSS_PAYLOAD!r});\n"
            + "process.stdout.write(out);\n"
        )
        result = subprocess.run(  # noqa: S603
            [node, "-e", script], capture_output=True, text=True, check=True
        )
        assert result.stdout == "&lt;img src=x onerror=alert(1)&gt;"
        assert "<" not in result.stdout


# ── what is served ───────────────────────────────────────────────────


class TestServedFiles:
    """Only the card is reachable under the integration's static URL."""

    async def test_only_www_is_registered(self) -> None:
        from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

        from custom_components.control4_dimmers.const import URL_BASE  # noqa: PLC0415
        from custom_components.control4_dimmers.frontend import (  # noqa: PLC0415
            WWW_DIR,
        )

        reg = JSModuleRegistration.__new__(JSModuleRegistration)
        reg.hass = MagicMock()
        reg.hass.http.async_register_static_paths = AsyncMock()
        await reg._async_register_path()
        [configs] = reg.hass.http.async_register_static_paths.await_args.args
        assert [(c.url_path, c.path) for c in configs] == [(URL_BASE, str(WWW_DIR))]
        assert CARD_JS.parent == WWW_DIR
        # Nothing but the card lives in the served directory.
        assert sorted(p.name for p in WWW_DIR.iterdir()) == [CARD_JS.name]


# ── card behavior under hostile stored data ──────────────────────────

_NODE_STUBS = r"""
globalThis.HTMLElement = class {};
globalThis.customElements = { define() {}, get() { return true; } };
globalThis.window = {
  customCards: [], addEventListener() {}, removeEventListener() {},
};
globalThis.document = { querySelectorAll() { return []; } };
globalThis.CSS = { escape: (s) => String(s) };
console.info = () => {};
"""

_NODE_CHECKS = r"""
const out = {};
const t0 = Date.now();
const huge = computeLayout([{ slot_id: 1, size: 1000000000 }], "keypad");
out.hugeSize = huge[0].size;
out.hugeMs = Date.now() - t0;
out.protoLayout = computeLayout([{ slot_id: 1 }], "constructor").length;
out.protoDefaults = defaultSlotsForType("__proto__").length;
out.badAction = typeof actionChipsHtml({ action: 5 }, null);
out.listTarget = actionChipsHtml(
  { action: "light.toggle", target: { entity_id: ["light.a", "light.b"] } }, null);
out.withData = actionChipsHtml(
  { action: "alarm_control_panel.alarm_disarm", data: { code: "1234" } }, null);
out.protoIcon = actionChipsHtml({ action: "constructor.x" }, null);
const el = { style: {} };
out.ledHex = setLedColor(el, "000 url(https://attacker.example/beacon)");
out.ledBg = el.style.background;
out.ownEntity = isOwnEntity(
  { entities: { "sensor.a": { platform: "control4_dimmers" },
                "sensor.b": { platform: "mqtt" } } }, "sensor.b");
process.stdout.write(JSON.stringify(out));
"""


class TestCardHardening:
    """Run the card's helpers in node against hostile stored values."""

    def _run(self, tmp_path: Path) -> dict:
        import json  # noqa: PLC0415

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        module = tmp_path / "card.mjs"
        module.write_text(_NODE_STUBS + CARD_JS.read_text() + _NODE_CHECKS)
        result = subprocess.run(  # noqa: S603
            [node, str(module)], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    def test_hostile_values_do_not_break_the_card(self, tmp_path: Path) -> None:
        out = self._run(tmp_path)
        assert out["hugeSize"] == 6
        assert out["hugeMs"] < 1000
        assert out["protoLayout"] == 0
        assert out["protoDefaults"] == 0
        assert out["badAction"] == "string"
        assert "light.a" in out["listTarget"]
        assert "light.b" in out["listTarget"]
        assert "+ data" in out["withData"]
        assert "1234" not in out["withData"]
        assert "mdi:cog" in out["protoIcon"]
        assert out["ledHex"] == "000000"
        assert out["ledBg"] == "#000000"
        assert out["ownEntity"] is False

    def test_no_unvalidated_led_or_selector_sinks(self) -> None:
        src = CARD_JS.read_text()
        # Every CSSOM LED color goes through setLedColor (which runs safeHex).
        assert src.count(".style.background =") == 1
        assert "querySelector(`.chassis" not in src
        # Type and icon tables are only read through ownGet.
        assert "DEVICE_TYPES[" not in src
        assert "DOMAIN_ICONS[" not in src
