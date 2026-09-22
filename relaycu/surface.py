"""Restricted semantic browser adapter; policy is owned by the executor.

Only allowlisted UI structure leaves this adapter through observations. Parameter
values stay in the live browser; extracted values return only to the caller.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Frame, Locator, Page, Playwright, async_playwright

from .models import Control, Observation, Step, Target


class SurfaceError(Exception):
    """Stable, safe failure information without browser exception contents."""

    def __init__(self, code: str, safe_message: str):
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Policy:
    """Trusted configuration, never loaded from a learned capability artifact."""

    origin: str = "http://127.0.0.1:4311"
    routes: frozenset[str] = field(default_factory=lambda: frozenset({
        "/", "/workspace", "/workspace/find", "/workspace/profile",
        "/workspace/accounts", "/workspace/savings", "/workspace/resume",
    }))
    frame_titles: frozenset[str] = field(default_factory=lambda: frozenset({"Member workspace"}))
    fill_labels: frozenset[str] = field(default_factory=lambda: frozenset({"Member ID"}))
    click_names: frozenset[str] = field(default_factory=lambda: frozenset({
        "Find member", "Open profile", "View accounts", "Open savings", "Retry",
    }))
    operator_click_names: frozenset[str] = field(default_factory=lambda: frozenset({"Restore session"}))
    extract_labels: frozenset[str] = field(default_factory=lambda: frozenset({"Current balance"}))
    heading_names: frozenset[str] = field(default_factory=lambda: frozenset({
        "Member search", "Search results", "Member profile", "Accounts", "Savings account",
        "Member services", "Find a member", "Member overview", "Member accounts",
        "Core banking", "Member workspace", "Session expired", "Service temporarily unavailable",
        "Member not found", "Validation error", "Permission denied", "Application error",
        "Unexpected confirmation",
    }))
    timeout_ms: int = 5000

    def __post_init__(self) -> None:
        if _origin(self.origin) != self.origin or urlsplit(self.origin).path:
            raise ValueError("Policy origin must be a canonical HTTP or HTTPS origin")
        if not self.routes or any(not route.startswith("/") or "?" in route or "#" in route for route in self.routes):
            raise ValueError("Policy routes must be absolute paths without query strings")
        if not 100 <= self.timeout_ms <= 30000:
            raise ValueError("Policy timeout is out of range")

    def permits_url(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            # URL paths are matched literally; no prefix, wildcard, or decoded fallback.
            return _origin(url) == self.origin and (parsed.path or "/") in self.routes
        except (ValueError, TypeError):
            return False


_CONDITIONS = (
    ("Unexpected confirmation", "unexpected_dialog"),
    ("Permission denied", "permission_denied"),
    ("Session expired", "session_expired"),
    ("Service temporarily unavailable", "transient"),
    ("Application error", "app_error"),
    ("Validation error", "validation"),
    ("Member not found", "not_found"),
)


class Surface:
    def __init__(self, policy: Policy | None = None):
        self.policy = policy or Policy()
        self.session_id = uuid4().hex
        self.page: Page | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._blocked = False
        self._dialog_seen = False
        self._control_targets: dict[str, Target] = {}
        self._event_tasks: set[asyncio.Task] = set()

    def _spawn_event(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def start(self, target_url: str, headed: bool = False) -> None:
        if self.page is not None:
            raise SurfaceError("already_started", "This browser session is already started.")
        if not self.policy.permits_url(target_url):
            raise SurfaceError("policy_blocked", "The destination is outside the trusted application policy.")
        self.session_id = uuid4().hex
        self._blocked = False
        self._dialog_seen = False
        try:
            self._playwright = await async_playwright().start()
            # A fresh browser, not a connection to an existing user's browser.
            self._browser = await self._playwright.chromium.launch(headless=not headed)
            self._context = await self._browser.new_context(
                accept_downloads=False, service_workers="block", viewport={"width": 1280, "height": 820},
            )
            self._context.set_default_timeout(self.policy.timeout_ms)
            self._context.set_default_navigation_timeout(self.policy.timeout_ms)
            await self._context.route("**/*", self._intercept_request)
            await self._context.route_web_socket("**/*", self._intercept_websocket)
            self.page = await self._context.new_page()
            self.page.on("dialog", self._on_dialog)
            self.page.on("download", lambda _: self._mark_blocked())
            self._context.on("page", self._on_extra_page)
            await self.page.goto(target_url, wait_until="load")
            await self._guard()
        except SurfaceError:
            await self.close()
            raise
        except Exception:
            blocked = self._blocked
            await self.close()
            if blocked:
                raise SurfaceError("policy_blocked", "The application attempted a destination outside policy.") from None
            raise SurfaceError("browser_unavailable", "The isolated application browser could not be started.") from None

    def _mark_blocked(self) -> None:
        self._blocked = True

    async def _intercept_request(self, route: Any) -> None:
        request = route.request
        if not self.policy.permits_url(request.url) or request.method not in {"GET", "HEAD", "POST"}:
            self._blocked = True
            await route.abort("blockedbyclient")
            return
        try:
            # continue_ permits the browser to follow redirect chains without
            # invoking this route handler again. Fetch without automatic redirects
            # and validate every hop BEFORE any request leaves the process.
            response = await route.fetch(max_redirects=0, timeout=self.policy.timeout_ms)
            current_url, method = request.url, request.method
            body = request.post_data_buffer
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in {"host", "cookie", "content-length"}}
            for _ in range(10):
                location = response.headers.get("location")
                if not (300 <= response.status < 400 and location):
                    await route.fulfill(response=response)
                    return
                destination = urljoin(current_url, location)
                if not self.policy.permits_url(destination):
                    self._blocked = True
                    await route.abort("blockedbyclient")
                    return
                if response.status == 303 and method != "HEAD" or response.status in {301, 302} and method == "POST":
                    method, body = "GET", None
                    headers.pop("content-type", None)
                current_url = destination
                response = await self._context.request.fetch(
                    destination, method=method, headers=headers, data=body,
                    max_redirects=0, timeout=self.policy.timeout_ms,
                )
            self._blocked = True
            await route.abort("blockedbyclient")
        except Exception:
            # No network exception (which can include POST data or URLs) escapes.
            self._blocked = True
            try:
                await route.abort("failed")
            except Exception:
                pass

    async def _intercept_websocket(self, route: Any) -> None:
        self._blocked = True
        await route.close(code=1008, reason="Connections are restricted by application policy")

    def _on_extra_page(self, page: Page) -> None:
        self._blocked = True
        page.on("dialog", self._on_dialog)
        self._spawn_event(page.close())

    def _on_dialog(self, dialog: Any) -> None:
        self._dialog_seen = True
        # Dismiss, never accept: release the browser wait while preserving a stop condition.
        self._spawn_event(dialog.dismiss())

    def _require_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            raise SurfaceError("session_closed", "The application browser session is unavailable.")
        return self.page

    async def _guard(self) -> None:
        page = self._require_page()
        if self._blocked or not self.policy.permits_url(page.url):
            raise SurfaceError("policy_blocked", "The application attempted a destination outside policy.")
        for frame in page.frames:
            if not self.policy.permits_url(frame.url):
                raise SurfaceError("policy_blocked", "An application frame is outside the trusted policy.")

    def _validate_target(self, target: Target) -> None:
        if target.frame is not None and target.frame not in self.policy.frame_titles:
            raise SurfaceError("policy_blocked", "The requested frame is outside the trusted policy.")

    async def _scope(self, target: Target) -> Frame:
        self._validate_target(target)
        page = self._require_page()
        if target.frame is None:
            return page.main_frame
        matches: list[Frame] = []
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            element = await frame.frame_element()
            if await element.get_attribute("title") == target.frame:
                matches.append(frame)
        if len(matches) != 1:
            code = "ambiguous_target" if len(matches) > 1 else "target_missing"
            raise SurfaceError(code, "The named application frame must resolve uniquely.")
        if not await (await matches[0].frame_element()).is_visible():
            raise SurfaceError("target_missing", "The named application frame is not visible.")
        return matches[0]

    async def _locator(self, target: Target) -> Locator:
        scope = await self._scope(target)
        if target.kind == "label":
            return scope.get_by_label(target.name, exact=True)
        if target.kind == "role":
            return scope.get_by_role(target.role, name=target.name, exact=True)
        return scope.get_by_text(target.name, exact=True)

    async def _unique_visible(self, target: Target) -> Locator:
        locator = await self._locator(target)
        count = await locator.count()
        if count != 1:
            code = "ambiguous_target" if count > 1 else "target_missing"
            raise SurfaceError(code, "The requested UI target must resolve uniquely.")
        if not await locator.is_visible():
            raise SurfaceError("target_missing", "The requested UI target is not visible.")
        return locator

    async def _scopes(self) -> list[tuple[str | None, Frame]]:
        page = self._require_page()
        scopes: list[tuple[str | None, Frame]] = [(None, page.main_frame)]
        for title in sorted(self.policy.frame_titles):
            matching = []
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                element = await frame.frame_element()
                if await element.get_attribute("title") == title:
                    matching.append(frame)
            if len(matching) > 1:
                raise SurfaceError("ambiguous_target", "The named application frame must resolve uniquely.")
            if matching and await (await matching[0].frame_element()).is_visible():
                scopes.append((title, matching[0]))
        return scopes

    async def observe(self) -> Observation:
        try:
            await self._guard()
            page = self._require_page()
            controls: list[Control] = []
            headings: list[str] = []
            condition = "unexpected_dialog" if self._dialog_seen else "ready"
            scopes = await self._scopes()
            self._control_targets = {}
            nonce = uuid4().hex[:10]
            for frame_name, scope in scopes:
                for heading in sorted(self.policy.heading_names):
                    locator = scope.get_by_role("heading", name=heading, exact=True)
                    if await self._any_visible(locator) and heading not in headings:
                        headings.append(heading)
                if condition == "ready":
                    for phrase, state in _CONDITIONS:
                        if await self._any_visible(scope.get_by_text(phrase, exact=True)):
                            condition = state
                            break
                targets: list[tuple[Target, str]] = []
                for label in sorted(self.policy.fill_labels):
                    targets.append((Target(kind="label", name=label, frame=frame_name), "fill"))
                for name in sorted(self.policy.click_names | self.policy.operator_click_names):
                    for role in ("button", "link"):
                        targets.append((Target(kind="role", role=role, name=name, frame=frame_name), "click"))
                for target, action in targets:
                    locator = await self._locator(target)
                    count = await locator.count()
                    if not count:
                        continue
                    if count > 1 and await self._any_visible(locator):
                        raise SurfaceError("ambiguous_target", "An actionable UI target must resolve uniquely.")
                    if count == 1 and await locator.is_visible() and await locator.is_enabled():
                        if action == "click" and await self._native_submission_blocked(locator):
                            continue
                        control_id = f"c_{nonce}_{len(controls)}"
                        controls.append(Control(id=control_id, target=target, actions=[action]))
                        self._control_targets[control_id] = target
            await self._guard()
            active_frame = scopes[-1][1]
            return Observation(route=urlsplit(active_frame.url).path or "/", headings=headings, controls=controls, condition=condition)
        except SurfaceError as error:
            if error.code == "policy_blocked":
                self._control_targets = {}
                return Observation(route="/blocked", headings=[], controls=[], condition="policy_blocked")
            raise
        except Exception:
            raise SurfaceError("observation_failed", "The application UI could not be inspected safely.") from None

    @staticmethod
    async def _any_visible(locator: Locator) -> bool:
        return any([await item.is_visible() for item in await locator.all()])

    @staticmethod
    async def _native_submission_blocked(locator: Locator) -> bool:
        """Observe native form validity without values or validation events.

        A submit button can be enabled while HTML constraints prevent submission.
        This reads current browser affordances; it does not prescribe a workflow.
        """
        return await locator.evaluate("""el => {
            const submit = (el.tagName === 'BUTTON' && el.type === 'submit') ||
                (el.tagName === 'INPUT' && ['submit', 'image'].includes(el.type));
            const form = el.form;
            if (!submit || !form || form.noValidate || el.formNoValidate) return false;
            return Array.from(form.elements).some(control =>
                control.willValidate && control.validity && !control.validity.valid);
        }""")

    def _authorize(self, step: Step, operator: bool) -> None:
        self._validate_target(step.target)
        target = step.target
        if step.action == "fill":
            if target.kind != "label" or target.name not in self.policy.fill_labels:
                raise SurfaceError("policy_blocked", "This input is outside the trusted action policy.")
        else:
            allowed = self.policy.click_names | (self.policy.operator_click_names if operator else frozenset())
            if target.kind != "role" or target.role not in {"button", "link"} or target.name not in allowed:
                raise SurfaceError("policy_blocked", "This action is outside the trusted action policy.")

    async def act(self, step: Step, params: dict) -> None:
        await self._act(step, params, operator=False)

    async def operator_act(self, step: Step, params: dict) -> None:
        await self._act(step, params, operator=True)

    async def _act(self, step: Step, params: dict, operator: bool) -> None:
        try:
            self._authorize(step, operator)
            await self._guard()
            if self._dialog_seen:
                raise SurfaceError("unexpected_dialog", "An unexpected confirmation requires stopping this session.")
            locator = await self._unique_visible(step.target)
            if not await locator.is_enabled():
                raise SurfaceError("target_disabled", "The requested UI target is disabled.")
            if step.action == "fill":
                value = params.get(step.input_ref)
                if type(value) is not str or not 0 < len(value) <= 100 or any(ord(char) < 32 for char in value):
                    raise SurfaceError("invalid_input", "The requested input must be a bounded text parameter.")
                await locator.fill(value)
            else:
                scope = await self._scope(step.target)
                # The application navigates its workspace iframe. Waiting only for
                # the already-loaded outer page races the next semantic lookup.
                async with scope.expect_navigation(wait_until="load", timeout=self.policy.timeout_ms):
                    await locator.click()
            await self._guard()
            if self._dialog_seen:
                raise SurfaceError("unexpected_dialog", "An unexpected confirmation requires stopping this session.")
        except SurfaceError:
            raise
        except Exception:
            if self._blocked:
                raise SurfaceError("policy_blocked", "The application attempted a destination outside policy.") from None
            if self._dialog_seen:
                raise SurfaceError("unexpected_dialog", "An unexpected confirmation requires stopping this session.") from None
            raise SurfaceError("action_failed", "The requested UI action did not complete safely.") from None

    async def is_visible(self, target: Target) -> bool:
        try:
            self._validate_read_target(target)
            await self._guard()
            locator = await self._locator(target)
            count = await locator.count()
            if count > 1:
                raise SurfaceError("ambiguous_target", "The requested UI target must resolve uniquely.")
            visible = count == 1 and await locator.is_visible()
            await self._guard()
            return visible
        except SurfaceError:
            raise
        except Exception:
            raise SurfaceError("inspection_failed", "The requested UI target could not be checked safely.") from None

    def _validate_read_target(self, target: Target) -> None:
        self._validate_target(target)
        allowed = (
            (target.kind == "label" and target.name in self.policy.extract_labels | self.policy.fill_labels)
            or (target.kind == "role" and target.role == "heading" and target.name in self.policy.heading_names)
            or (target.kind == "role" and target.role in {"button", "link"} and target.name in self.policy.click_names | self.policy.operator_click_names)
            or (target.kind == "text" and target.name in {phrase for phrase, _ in _CONDITIONS})
        )
        if not allowed:
            raise SurfaceError("policy_blocked", "This read target is outside the trusted UI policy.")

    async def extract(self, target: Target) -> str:
        try:
            if target.kind != "label" or target.name not in self.policy.extract_labels:
                raise SurfaceError("policy_blocked", "This output is outside the trusted extraction policy.")
            await self._guard()
            locator = await self._unique_visible(target)
            # Reading the designated element is intentional. Never persist it or include
            # it in observations, snapshots, logs, or exception messages.
            tag = await locator.evaluate("element => element.tagName.toLowerCase()")
            value = await locator.input_value() if tag in {"input", "textarea", "select"} else await locator.inner_text()
            await self._guard()
            if len(value) > 200:
                raise SurfaceError("invalid_output", "The output exceeded its permitted size.")
            return value.strip()
        except SurfaceError:
            raise
        except Exception:
            raise SurfaceError("extraction_failed", "The designated output could not be read safely.") from None

    async def safe_snapshot(self) -> dict:
        observation = await self.observe()
        snapshot: dict[str, Any] = {
            "observation": observation.model_dump(),
            "structure": {"visible_frames": 0, "visible_controls": 0, "frames": []},
        }
        if observation.condition == "policy_blocked":
            return snapshot
        try:
            async def describe(target: Target, locator: Locator, *, control: bool) -> dict | None:
                count = await locator.count()
                if not count:
                    return None
                elements = []
                # Bound evidence size even if an application duplicates controls.
                for index in range(min(count, 16)):
                    element = locator.nth(index)
                    state = {"visible": await element.is_visible()}
                    if control:
                        state["enabled"] = await element.is_enabled()
                    elements.append(state)
                return {"target": target.model_dump(exclude_none=True),
                        "match_count": count, "elements": elements,
                        "sample_truncated": count > len(elements)}

            scopes = await self._scopes()
            snapshot["structure"]["visible_frames"] = max(0, len(scopes) - 1)
            for frame_name, frame in scopes:
                # Counts can include unknown controls; identities come only from
                # trusted policy names, never text or attributes read from the UI.
                visible_controls = await frame.locator("input,select,textarea,button,a[href]").evaluate_all(
                    "elements => elements.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden').length"
                )
                snapshot["structure"]["visible_controls"] += visible_controls
                structure = {"scope": frame_name or "main", "visible": True,
                             "visible_controls": visible_controls, "headings": [], "controls": []}
                for name in sorted(self.policy.heading_names):
                    target = Target(kind="role", role="heading", name=name, frame=frame_name)
                    entry = await describe(target, frame.get_by_role("heading", name=name, exact=True, include_hidden=True), control=False)
                    if entry:
                        structure["headings"].append(entry)
                for name in sorted(self.policy.fill_labels):
                    target = Target(kind="label", name=name, frame=frame_name)
                    entry = await describe(target, frame.get_by_label(name, exact=True), control=True)
                    if entry:
                        structure["controls"].append(entry)
                for name in sorted(self.policy.click_names | self.policy.operator_click_names):
                    for role in ("button", "link"):
                        target = Target(kind="role", role=role, name=name, frame=frame_name)
                        entry = await describe(target, frame.get_by_role(role, name=name, exact=True, include_hidden=True), control=True)
                        if entry:
                            structure["controls"].append(entry)
                snapshot["structure"]["frames"].append(structure)
            await self._guard()
            return snapshot
        except SurfaceError:
            raise
        except Exception:
            raise SurfaceError("snapshot_failed", "A sanitized UI snapshot could not be produced.") from None

    async def close(self) -> None:
        for task in tuple(self._event_tasks):
            if not task.done():
                task.cancel()
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self.page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._control_targets = {}
