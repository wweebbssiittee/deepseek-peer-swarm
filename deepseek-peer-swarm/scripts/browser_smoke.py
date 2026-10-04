"""Offline dashboard integration check; requires the optional Playwright package.

    python scripts/browser_smoke.py --browser /path/to/chrome

Starts its own temporary server, uses synthetic provider responses, checks the
full task lifecycle, and writes privacy-safe screenshots. No real API keys,
existing runtime, paid model calls or external tools are used. ``--demo`` is an
explicit alias for this default behavior; there is no live-run mode.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from browser_fixture import isolated_server, public_fixture
from playwright.sync_api import expect, sync_playwright


def run_smoke(args, home, provider) -> None:
    output = Path(args.screenshots)
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    external_requests: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True,
                                    args=["--disable-background-networking"])
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def offline_only(route):
            if route.request.url.startswith(args.url + "/"):
                route.continue_()
            else:
                external_requests.append(urlparse(route.request.url).hostname or "unknown")
                route.abort()

        def clean_response(route):
            response = route.fetch()
            route.fulfill(response=response, json=public_fixture(response.json(), home))

        page.route("**/*", offline_only)
        page.route(f"{args.url}/api/**", clean_response)
        page.goto(args.url, wait_until="networkidle")
        expect(page.locator("#connection-status")).to_have_text("Local server connected")
        if args.check_budget_sound:
            original_sound = page.locator("#sound-toggle").get_attribute("aria-checked") == "true"
            try:
                if original_sound:
                    page.locator("#sound-toggle").click()
                expect(page.locator("#sound-toggle")).to_have_attribute("aria-checked", "false")
                page.reload(wait_until="networkidle")
                expect(page.locator("#sound-toggle")).to_have_attribute("aria-checked", "false")
                tested_sound = []

                def silent_sound_test(route):
                    tested_sound.append(True)
                    route.fulfill(json={"enabled": False, "available": True, "last_error": None})

                page.route("**/api/notifications/test", silent_sound_test)
                page.locator("#sound-test").click()
                expect(page.locator("#toast")).to_have_text("Test sound sent to Windows.")
                assert tested_sound == [True], "Sound test must use the local endpoint exactly once"
                page.unroute("**/api/notifications/test", silent_sound_test)
                if original_sound:
                    page.locator("#sound-toggle").click()
                    expect(page.locator("#sound-toggle")).to_have_attribute("aria-checked", "true")
            finally:
                # Restore the isolated fixture preference even if a check fails.
                token = page.request.get(f"{args.url}/api/bootstrap").json()["csrf_token"]
                restored = page.request.post(f"{args.url}/api/notifications", data={"enabled": original_sound}, headers={"X-Swarm-Token": token})
                assert restored.ok, "Could not restore the original sound preference"
        expect(page.locator("#toast")).not_to_be_visible(timeout=8000)
        page.screenshot(path=str(output / "overview.png"), full_page=True)

        if args.demo:
            page.locator('[data-action="new-run"]').first.click()
            page.locator("#task-input").fill("[OFFLINE FIXTURE] Ten equal peers share and review work.")
            initial_tokens = page.locator("#task-tokens").input_value()
            if args.check_budget_sound:
                expect(page.locator("#task-budget-usd")).to_have_value("1.00")
                expect(page.locator("#task-stall-minutes")).to_have_value("10")
                page.locator("#task-budget-usd").fill("0.37")
            page.locator('#task-permissions [name="internet"]').uncheck()
            page.locator('#task-permissions [name="commands"]').select_option("deny")
            page.locator('#task-permissions [name="deploy"]').select_option("deny")
            page.locator("#create-run").click()
            expect(page.locator("#task-dialog")).not_to_be_visible(timeout=15000)
            expect(page.locator("#run-status")).to_have_text("running")
            expect(page.locator(".agent-card")).to_have_count(10)
            expect(page.locator("#message-list")).to_contain_text("Peer 10: ready to share and review work", timeout=15000)
            if args.check_budget_sound:
                expect(page.locator("#billing-budget")).to_have_text("$0.37")
                expect(page.locator("#billing-spent")).to_have_text("$0.00")
                expect(page.locator("#billing-remaining")).to_have_text("$0.37")
            page.locator("#pause-run").click()
            expect(page.locator("#run-status")).to_have_text("paused", timeout=15000)
            page.locator("#chat-input").fill("Please keep the result concise and show peer review evidence.")
            page.locator("#chat-input").press("Control+Enter")
            expect(page.locator("#message-list")).to_contain_text("Please keep the result concise", timeout=15000)
            expect(page.locator("#run-workspace")).to_have_text(re.compile(r"^demo-workspace"))
            page.evaluate("window.scrollTo(0, 0)")
            expect(page.locator("#toast")).not_to_be_visible(timeout=8000)
            page.screenshot(path=str(output / "desktop.png"), full_page=True)

            page.locator("#tab-board").click()
            expect(page.locator("#panel-board")).to_be_visible()
            page.locator("#tab-activity").click()
            expect(page.locator("#activity-list .activity-event").first).to_be_visible()
            page.locator("#activity-list .activity-event summary").first.click()
            expect(page.locator("#activity-list .activity-event[open] .event-payload")).to_be_visible()
            page.locator("#tab-chat").click()

            # Exercise approval buttons against a browser-only fixture. This must
            # never reach the engine or execute a command on the computer.
            run_id = parse_qs(urlparse(page.url).query)["run"][0]
            snapshot_pattern = f"{args.url}/api/runs/{run_id}"
            approval_pattern = f"{args.url}/api/runs/{run_id}/approvals/browser-smoke-approval"
            approval_state = {"pending": True, "decisions": []}
            budget_fixture = {"enabled": args.check_budget_sound}

            def snapshot_with_approval(route):
                response = route.fetch()
                body = public_fixture(response.json(), home)
                if approval_state["pending"]:
                    body["approvals"].append({"id": "browser-smoke-approval", "agent_id": "peer-03", "category": "commands", "status": "pending", "payload": {"command": "echo browser-only approval fixture", "cwd": "browser-smoke-workspace"}})
                if budget_fixture["enabled"]:
                    body["billing"].update({"spent_usd": "0.123456", "reserved_usd": "0.010000", "uncertain_usd": "0.020000", "remaining_usd": "0.216544", "legacy_unpriced": True})
                    body["run"]["attention"] = "Browser-only fixture: this task needs your input."
                route.fulfill(response=response, json=body)

            def decide_fixture(route):
                approval_state["decisions"].append(route.request.post_data_json["approved"])
                approval_state["pending"] = False
                route.fulfill(json={"ok": True})

            page.route(snapshot_pattern, snapshot_with_approval)
            page.route(approval_pattern, decide_fixture)
            expect(page.locator("#approval-section")).to_be_visible(timeout=10000)
            expect(page.locator(".approval-payload")).to_contain_text("echo browser-only approval fixture")
            if args.check_budget_sound:
                expect(page.locator("#billing-spent")).to_have_text("$0.123456")
                expect(page.locator("#billing-held")).to_have_text("$0.03")
                expect(page.locator("#billing-remaining")).to_have_text("$0.216544")
                expect(page.locator("#billing-legacy")).to_be_visible()
                expect(page.locator("#run-attention-text")).to_have_text("Browser-only fixture: this task needs your input.")
                page.locator(".billing-details summary").click()
                expect(page.locator("#billing-held-detail")).to_contain_text("Unconfirmed usage: $0.02 held")
                expect(page.locator("#billing-pricing")).to_contain_text("cache-hit input")
                page.locator(".billing-details summary").click()
            page.locator('[data-approval="browser-smoke-approval"][data-approved="true"]').click()
            expect(page.locator("#approval-section")).not_to_be_visible(timeout=10000)
            approval_state["pending"] = True
            expect(page.locator("#approval-section")).to_be_visible(timeout=10000)
            page.locator('[data-approval="browser-smoke-approval"][data-approved="false"]').click()
            expect(page.locator("#approval-section")).not_to_be_visible(timeout=10000)
            assert approval_state["decisions"] == [True, False]
            page.unroute(snapshot_pattern, snapshot_with_approval)
            page.unroute(approval_pattern, decide_fixture)
            if args.check_budget_sound:
                expect(page.locator("#billing-spent")).to_have_text("$0.00", timeout=10000)
                expect(page.locator("#billing-legacy")).not_to_be_visible()

            page.locator("#edit-permissions").click()
            expect(page.locator('#run-permissions [name="write_files"]')).not_to_be_checked()
            expect(page.locator('#run-permissions [name="commands"]')).to_have_value("deny")
            page.locator("#save-permissions").click()
            expect(page.locator("#permissions-dialog")).not_to_be_visible()

            page.locator("#edit-limits").click()
            expect(page.locator("#run-tokens")).to_have_value(initial_tokens)
            if args.check_budget_sound:
                expect(page.locator("#run-budget-usd")).to_have_value("0.37")
                page.locator("#run-budget-usd").fill("0.50")
                expect(page.locator("#run-stall-minutes")).to_have_value("10")
                page.locator("#run-stall-minutes").fill("15")
            if args.check_limits:
                page.locator("#run-tokens").fill("2100000")
                page.locator("#run-output-tokens").fill("12288")
                page.locator("#save-limits").click()
                expect(page.locator("#limits-dialog")).not_to_be_visible()
                expect(page.locator("#run-budget")).to_contain_text(page.evaluate("Number(2100000).toLocaleString()"))
                page.locator("#edit-limits").click()
                expect(page.locator("#run-output-tokens")).to_have_value("12288")
                if args.check_budget_sound:
                    expect(page.locator("#run-budget-usd")).to_have_value("0.50")
                    expect(page.locator("#run-stall-minutes")).to_have_value("15")
                    expect(page.locator("#billing-budget")).to_have_text("$0.50")
                page.locator('[data-close="limits-dialog"]').first.click()
            elif args.check_budget_sound:
                page.locator("#save-limits").click()
                expect(page.locator("#limits-dialog")).not_to_be_visible()
                expect(page.locator("#billing-budget")).to_have_text("$0.50")
            else:
                page.locator('[data-close="limits-dialog"]').first.click()

            page.set_viewport_size({"width": 1440, "height": 1050})
            page.locator("#pause-run").click()
            expect(page.locator("#run-status")).to_have_text("running", timeout=15000)
            page.locator("#stop-run").click()
            expect(page.locator("#run-status")).to_have_text("stopped", timeout=15000)
            expect(page.locator("#chat-input")).to_be_disabled()

        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("window.scrollTo(0, 0)")
        expect(page.locator("#toast")).not_to_be_visible(timeout=8000)
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows horizontally"
        page.locator("#open-settings").click()
        expect(page.locator('#api-key-fields input[type="password"]')).to_have_count(10)
        expect(page.locator("#settings-model")).not_to_have_value("")
        expect(page.locator("#settings-output-tokens")).to_have_value("8192")
        page.screenshot(path=str(output / "mobile-settings.png"))
        page.locator('[data-close="settings-dialog"]').first.click()

        assert not errors, f"Browser script errors: {errors}"
        assert not external_requests, f"Unexpected external browser requests: {external_requests}"
        assert provider.calls >= 10, "Every peer must use the fake provider"
        print(json.dumps({"ok": True, "screenshots": str(output.resolve()), "javascript_errors": errors,
                          "external_browser_requests": external_requests, "fake_provider_calls": provider.calls,
                          "mode": "isolated-offline-fixture", "limits_checked": args.check_limits,
                          "budget_sound_checked": args.check_budget_sound}))
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parser.add_argument("--screenshots", default=".browser-check")
    parser.add_argument("--demo", action="store_true", default=True,
                        help="Use isolated fake-provider state (always enabled)")
    parser.add_argument("--check-limits", action="store_true", default=True,
                        help="Exercise limit changes (always enabled)")
    parser.add_argument("--check-budget-sound", action="store_true", default=True,
                        help="Exercise billing and silent notification controls (always enabled)")
    args = parser.parse_args()
    with isolated_server() as (url, home, provider):
        args.url = url
        run_smoke(args, home, provider)


if __name__ == "__main__":
    main()
