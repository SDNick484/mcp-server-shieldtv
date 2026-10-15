"""doctor, simulate and call, against the simulated Shield."""

from __future__ import annotations

import asyncio
import json

import pytest

from shieldtv_mcp import cli
from shieldtv_mcp.config import load_settings
from shieldtv_mcp.doctor import STEP, render, run_doctor, to_json

pytestmark = pytest.mark.anyio


async def doctor():
    return await run_doctor(load_settings(), timeout=1.0, finder=None)


def layers(report) -> dict[str, bool]:
    return {c.layer: c.ok for c in report.checks}


async def test_doctor_all_good(paired):
    report = await doctor()
    assert report.ok and layers(report) == {"config": True, "tcp": True, "identity": True, "session": True}
    text = render(report)
    assert "SHIELD Android TV, MAC xx:xx:xx:xx:B2:C3" in text  # MACs redacted by default
    assert "00:04:4B:A1:B2:C3" in render(report, redacted=False)
    assert json.loads(to_json(report))["ok"] is True
    assert paired.keys == [] and paired.launched == []  # read-only


async def test_doctor_not_paired(home, shield):
    report = await doctor()
    assert not report.ok and report.checks[0].layer == "config"
    assert "mcp-server-shieldtv pair" in report.checks[-1].hint
    assert f"step {STEP['config']}" in report.checks[-1].hint


async def test_doctor_loose_permissions(home, paired):
    (home / "key.pem").chmod(0o644)
    report = await doctor()
    assert not report.ok and "readable by other users: key.pem" in report.checks[0].detail


async def test_doctor_shield_off_the_network(paired):
    await paired.stop()
    report = await doctor()
    assert layers(report) == {"config": True, "tcp": False}


async def test_doctor_another_device_at_the_address(home, paired):
    saved = json.loads((home / "config.json").read_text())
    (home / "config.json").write_text(json.dumps({**saved, "mac": "00:04:4B:00:00:01"}))
    report = await doctor()
    assert layers(report)["identity"] is False
    assert "now belongs to another device" in report.checks[-1].hint


async def test_doctor_unpaired_on_the_tv(paired):
    paired.forget()
    report = await doctor()
    assert layers(report)["session"] is False
    assert "rejected our certificate" in report.checks[-1].detail


async def test_doctor_session_never_starts(paired):
    paired.faults.no_start = True
    report = await doctor()
    assert layers(report)["session"] is False and report.checks[-1].detail == "no remote session started"


# --- simulate + call -----------------------------------------------------------------------
async def test_simulate_paired_then_call(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SHIELDTV_HOST", raising=False)
    monkeypatch.delenv("SHIELDTV_DRY_RUN", raising=False)
    monkeypatch.setenv("SHIELDTV_CONFIG_DIR", str(tmp_path))  # simulate sets it too; restored after
    args = cli.build_parser().parse_args(["simulate", "--config-dir", str(tmp_path), "--paired"])
    sim = asyncio.ensure_future(cli._cmd_simulate(args))
    try:
        for _ in range(100):
            if "Ctrl+C" in capsys.readouterr().out or (tmp_path / "cert.pem").exists():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        assert await cli._cmd_call("launch_app", ["app=youtube"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["outcome"] == "done" and result["shield"] == "SHIELD Android TV"
        assert await cli._cmd_call("send_key", ["key=POWER"]) == 1  # not on the allow-list
        assert "Input should be" in capsys.readouterr().err
    finally:
        sim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sim


async def test_call_rejects_malformed_arguments(home, capsys):
    assert await cli._cmd_call("send_key", ["HOME"]) == 2
    assert "key=value" in capsys.readouterr().err
