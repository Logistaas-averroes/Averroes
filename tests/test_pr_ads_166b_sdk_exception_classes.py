"""PR-ADS-166B — every HubSpot SDK call is guarded by ITS package's exceptions.

The hubspot-api-client SDK generates one ``ApiException`` class per API
package (``hubspot.crm.contacts``, ``hubspot.crm.deals``,
``hubspot.crm.companies`` …) and they share no base class. Transport failures
(a timeout, a refused connection) are not SDK exceptions at all: the SDK lets
the raw ``urllib3`` exception through.

``pull_deals_for_ledger`` called the DEALS search API and caught the CONTACTS
``ApiException``. So a deals 429 was never retried, and a 5xx or a timeout
never took the "partial, and said to be partial" path: every one escaped the
connector. ``pull_contact_attribution_properties`` caught
``requests.exceptions.RequestException`` around an SDK call, which the SDK
never raises, so a timeout there was never retried.

The runtime tests below do not hand-build exceptions where they can avoid it.
They drive the REAL SDK client and replace only the socket layer
(``urllib3.PoolManager.request``), so the exception that reaches the connector
is the one the SDK itself raises in production for that HTTP status — its
class, its package, its ``status``. The structural guard reads
``connectors/hubspot_pull.py`` and fails on any ``except`` around a
``client.crm.<package>`` call that does not name that package's class or the
shared tuple.
"""

from __future__ import annotations

import ast
import functools
import json
import pathlib
from datetime import datetime, timezone

import hubspot
import pytest
import urllib3

import connectors.hubspot_pull as hubspot_pull

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "connectors" / "hubspot_pull.py"

DEALS_SEARCH_PATH = "/crm/v3/objects/deals/search"
CONTACTS_BATCH_READ_PATH = "/crm/v3/objects/contacts/batch/read"


# ─────────────────────────────────────────────────────────────────────────────
# A real SDK client over a scripted socket layer
# ─────────────────────────────────────────────────────────────────────────────
def _http(status: int, payload=None, reason: str = "") -> urllib3.HTTPResponse:
    body = json.dumps(payload if payload is not None else {}).encode()
    return urllib3.HTTPResponse(
        body=body, status=status, reason=reason or "",
        headers={"Content-Type": "application/json"}, preload_content=True)


def _deal(deal_id: str, modified: str) -> dict:
    return {"id": deal_id,
            "properties": {"dealname": f"Deal {deal_id}",
                           "dealstage": "326093516",
                           "hs_is_closed": "true", "hs_is_closed_won": "true",
                           "hs_lastmodifieddate": modified,
                           "amount": "100", "deal_currency_code": "USD"},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": modified, "archived": False}


def _search_page(deals: list, next_after: str | None = None) -> dict:
    page = {"total": len(deals), "results": deals}
    if next_after is not None:
        page["paging"] = {"next": {"after": next_after}}
    return page


def _full_page() -> dict:
    """A FULL first page (100 deals, ascending distinct timestamps). Under
    keyset paging only a full page makes the read ask again; a short page is
    the end of the result set. ``total`` says more deals remain."""
    deals = [_deal(str(1000 + i), f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}Z")
             for i in range(100)]
    return {"total": 150, "results": deals}


LAST_OF_FULL_PAGE = "2026-11-01T00:01:39Z"


def _timeout(url: str) -> urllib3.exceptions.ReadTimeoutError:
    return urllib3.exceptions.ReadTimeoutError(None, url, "Read timed out.")


class _Transport:
    """Answers the SDK's HTTP requests from a script, one entry per request.

    An entry is an ``HTTPResponse`` (returned) or an exception (raised, as
    urllib3 raises it). Every request is recorded with its path and the
    timeout the SDK passed down.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []

    def __call__(self, method, url, *args, **kwargs):
        self.calls.append({"method": method, "url": url,
                           "timeout": kwargs.get("timeout")})
        if not self.script:
            raise AssertionError(f"unscripted HubSpot request: {method} {url}")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def paths(self) -> list:
        return [c["url"].split("api.hubapi.com", 1)[-1] for c in self.calls]


@pytest.fixture
def sdk(monkeypatch):
    """Install a real ``hubspot.Client`` whose sockets follow a script."""
    monkeypatch.setattr(hubspot_pull.time, "sleep", lambda *_: None)

    def _install(script):
        transport = _Transport(script)
        # A plain function, so it binds as a method of every PoolManager.
        monkeypatch.setattr(
            urllib3.PoolManager, "request",
            lambda _pool, method, url, *a, **kw: transport(method, url, *a, **kw))
        client = hubspot.Client.create(access_token="test-token")
        monkeypatch.setattr(hubspot_pull, "get_client", lambda: client)
        return transport

    return _install


# ─────────────────────────────────────────────────────────────────────────────
# 1. The premise: one class per package, no common base
# ─────────────────────────────────────────────────────────────────────────────
def test_1_a_each_sdk_package_raises_its_own_api_exception_with_no_common_base():
    from hubspot.crm.companies import ApiException as CompaniesApiException
    from hubspot.crm.contacts import ApiException as ContactsApiException
    from hubspot.crm.deals import ApiException as DealsApiException
    from hubspot.crm.deals.exceptions import ForbiddenException, ServiceException

    assert DealsApiException is not ContactsApiException
    assert not issubclass(DealsApiException, ContactsApiException)
    assert not issubclass(CompaniesApiException, ContactsApiException)
    assert not issubclass(ServiceException, ContactsApiException)
    assert not issubclass(ForbiddenException, ContactsApiException)
    for cls in (ContactsApiException, DealsApiException, CompaniesApiException):
        assert cls in hubspot_pull.HUBSPOT_API_EXCEPTIONS


def test_1_b_the_real_sdk_raises_the_deals_class_for_a_deals_429_and_lets_a_timeout_through(sdk):
    """What the fakes below rely on: the SDK, not the test, picks the class."""
    from hubspot.crm.deals import ApiException as DealsApiException
    from hubspot.crm.contacts import ApiException as ContactsApiException

    sdk([_http(429, {"status": "error"}, "Too Many Requests"),
         _timeout("https://api.hubapi.com" + DEALS_SEARCH_PATH)])
    client = hubspot_pull.get_client()
    with pytest.raises(DealsApiException) as raised:
        client.crm.deals.search_api.do_search(public_object_search_request={})
    assert raised.value.status == 429
    assert not isinstance(raised.value, ContactsApiException)
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        client.crm.deals.search_api.do_search(public_object_search_request={})


# ─────────────────────────────────────────────────────────────────────────────
# 2. pull_deals_for_ledger — every outcome stays inside the connector contract
# ─────────────────────────────────────────────────────────────────────────────
def test_2_a_a_deals_429_on_every_attempt_is_retried_then_reported_partial_not_raised(sdk):
    budget = hubspot_pull.MAX_RETRIES + 1
    transport = sdk([_http(429, {"status": "error"}, "Too Many Requests")] * budget)

    pull = hubspot_pull.pull_deals_for_ledger()

    assert transport.paths() == [DEALS_SEARCH_PATH] * budget
    assert pull["available"] is True
    assert pull["complete"] is False
    assert pull["deals"] == []
    assert "HTTP 429" in pull["error"]
    assert f"after {budget} attempts" in pull["error"]


def test_2_b_a_deals_429_followed_by_an_answer_is_retried_and_completes(sdk):
    transport = sdk([
        _http(429, {"status": "error"}, "Too Many Requests"),
        _http(200, _search_page([_deal("D1", "2026-11-01T00:00:00Z")])),
    ])

    pull = hubspot_pull.pull_deals_for_ledger()

    assert len(transport.calls) == 2
    assert pull["complete"] is True and pull["error"] is None
    assert [d["id"] for d in pull["deals"]] == ["D1"]


def test_2_c_a_deals_503_after_a_good_page_is_partial_and_keeps_the_page_already_read(sdk):
    budget = hubspot_pull.MAX_RETRIES + 1
    sdk([_http(200, _full_page())]
        + [_http(503, {"status": "error"}, "Service Unavailable")] * budget)

    pull = hubspot_pull.pull_deals_for_ledger()

    assert pull["available"] is True
    assert pull["complete"] is False, "a 503 mid-scan was reported complete"
    assert pull["pages"] == 1
    assert [d["id"] for d in pull["deals"]] == [str(1000 + i) for i in range(100)]
    assert "HTTP 503" in pull["error"] and "on page 2" in pull["error"]


def test_2_d_a_deals_read_timeout_is_retried_and_then_partial_not_an_escaped_urllib3_error(sdk):
    budget = hubspot_pull.MAX_RETRIES + 1
    url = "https://api.hubapi.com" + DEALS_SEARCH_PATH
    transport = sdk([_timeout(url)] * budget)

    pull = hubspot_pull.pull_deals_for_ledger()

    assert len(transport.calls) == budget
    assert pull["available"] is True and pull["complete"] is False
    assert "ReadTimeoutError" in pull["error"]


def test_2_e_a_deals_max_retry_error_then_an_answer_completes(sdk):
    url = "https://api.hubapi.com" + DEALS_SEARCH_PATH
    sdk([urllib3.exceptions.MaxRetryError(None, url, reason="connection refused"),
         _http(200, _search_page([_deal("D1", "2026-11-01T00:00:00Z")]))])

    pull = hubspot_pull.pull_deals_for_ledger()

    assert pull["complete"] is True
    assert [d["id"] for d in pull["deals"]] == ["D1"]


def test_2_f_a_deals_403_is_not_retried_and_fails_loudly_with_the_http_status(sdk):
    transport = sdk([_http(403, {"status": "error",
                                 "message": "missing scope crm.objects.deals.read"},
                           "Forbidden")])

    pull = hubspot_pull.pull_deals_for_ledger()

    assert len(transport.calls) == 1, "a refusal was retried"
    assert pull["available"] is False, "a 403 was reported as a readable partial"
    assert pull["complete"] is False and pull["deals"] == []
    assert "HTTP 403" in pull["error"]


def test_2_g_the_deals_search_passes_the_request_timeout_to_the_socket(sdk):
    transport = sdk([_http(200, _search_page([]))])

    hubspot_pull.pull_deals_for_ledger()

    timeout = transport.calls[0]["timeout"]
    assert timeout is not None, "the deals search has no timeout"
    assert timeout.total == hubspot_pull.HUBSPOT_REQUEST_TIMEOUT_SECONDS


# ─────────────────────────────────────────────────────────────────────────────
# 3. What the deal-ledger sync makes of each outcome
# ─────────────────────────────────────────────────────────────────────────────
class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _Cursor(self.sink)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def ledger(monkeypatch):
    """The real service and the REAL ``record_sync_state`` over a recording
    connection: the watermark decision is the repository's, not the test's."""
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    state_params: list = []
    upserts: list = []
    monkeypatch.setattr(repo, "get_conn", lambda: _Conn(state_params))
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {
        "available": True,
        "row": {"last_modified_watermark": "2026-10-01T00:00:00+00:00"}})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: (
        upserts.append(row) or {"available": True, "written": 1,
                                "skipped_stale": 0, "error": None}))
    monkeypatch.setattr(svc, "_fx_rates_for", lambda *a, **k: {})
    monkeypatch.setattr(hubspot_pull, "fetch_portal_home_currency", lambda: {
        "available": True, "home_currency_code": "USD", "verified": True})
    monkeypatch.setattr(hubspot_pull, "fetch_deal_associations", lambda deal_id: {
        "deal_id": deal_id, "contacts": [], "complete": True})
    return {"svc": svc, "state": state_params, "upserts": upserts}


def test_3_a_an_exhausted_deals_429_on_the_first_page_makes_the_sync_failed_and_moves_no_watermark(
        sdk, ledger):
    """Review 1-M2: a run that read no page learned nothing; `partial` would
    say some of the population was read."""
    budget = hubspot_pull.MAX_RETRIES + 1
    sdk([_http(429, {"status": "error"}, "Too Many Requests")] * budget)

    result = ledger["svc"].sync_deals(batch_id=7)

    assert result["status"] == "failed"
    assert "HTTP 429" in result["error"]
    assert result["watermark"] is None
    (params,) = ledger["state"]
    assert params["status"] == "failed"
    assert params["advance"] is False, "a rate-limited read advanced the watermark"
    assert params["watermark"] is None


def test_3_b_a_deals_403_makes_the_sync_failed_with_the_status_in_its_error(sdk, ledger):
    sdk([_http(403, {"status": "error"}, "Forbidden")])

    result = ledger["svc"].sync_deals(batch_id=7)

    assert result["status"] == "failed"
    assert "HTTP 403" in result["error"]
    (params,) = ledger["state"]
    assert params["status"] == "failed"
    assert params["advance"] is False
    assert "HTTP 403" in params["error"]
    assert ledger["upserts"] == []


def test_3_c_a_429_after_one_good_page_checkpoints_only_the_clean_prefix_and_never_proves_freshness(
        sdk, ledger):
    from services.canonical_customer_revenue_service import freshness_block

    budget = hubspot_pull.MAX_RETRIES + 1
    sdk([_http(200, _full_page())]
        + [_http(429, {"status": "error"}, "Too Many Requests")] * budget)

    result = ledger["svc"].sync_deals(batch_id=7)

    assert result["status"] == "partial"
    assert len(ledger["upserts"]) == 100
    (params,) = ledger["state"]
    assert params["status"] == "partial"
    # The documented checkpoint: the clean, committed, ascending prefix only.
    assert result["watermark_is_checkpoint"] is True
    assert params["watermark"] == LAST_OF_FULL_PAGE
    # A partial run is never the successful incremental the freshness gate reads.
    block = freshness_block(
        {"last_sync_mode": "incremental", "last_status": params["status"],
         "last_incremental_at": "2026-11-19T00:00:00+00:00",
         "bootstrap_status": "complete"},
        [], datetime(2026, 11, 19, 1, tzinfo=timezone.utc))
    assert block["latest_successful_incremental_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. pull_contact_attribution_properties — a timeout is retried
# ─────────────────────────────────────────────────────────────────────────────
def _contacts_batch(ids) -> dict:
    return {"status": "COMPLETE",
            "results": [{"id": cid, "properties": {"hs_analytics_source": "PAID_SEARCH"},
                         "createdAt": "2026-01-01T00:00:00Z",
                         "updatedAt": "2026-01-01T00:00:00Z", "archived": False}
                        for cid in ids],
            "startedAt": "2026-01-01T00:00:00Z",
            "completedAt": "2026-01-01T00:00:01Z"}


def test_4_a_a_contacts_batch_read_timeout_is_retried_like_an_api_error(sdk):
    url = "https://api.hubapi.com" + CONTACTS_BATCH_READ_PATH
    transport = sdk([_timeout(url), _http(200, _contacts_batch(["C1"]))])

    out = hubspot_pull.pull_contact_attribution_properties(["C1"])

    assert transport.paths() == [CONTACTS_BATCH_READ_PATH] * 2
    assert out["C1"]["properties"]["hs_analytics_source"] == "PAID_SEARCH"


def test_4_b_a_contacts_batch_read_that_always_times_out_raises_the_named_error(sdk):
    url = "https://api.hubapi.com" + CONTACTS_BATCH_READ_PATH
    transport = sdk([_timeout(url)] * hubspot_pull.MAX_RETRIES)

    with pytest.raises(hubspot_pull.ContactBatchReadError):
        hubspot_pull.pull_contact_attribution_properties(["C1"])
    assert len(transport.calls) == hubspot_pull.MAX_RETRIES


def test_4_c_the_contacts_batch_read_passes_the_request_timeout_to_the_socket(sdk):
    transport = sdk([_http(200, _contacts_batch(["C1"]))])

    hubspot_pull.pull_contact_attribution_properties(["C1"])

    timeout = transport.calls[0]["timeout"]
    assert timeout is not None and timeout.total == \
        hubspot_pull.HUBSPOT_REQUEST_TIMEOUT_SECONDS


# ─────────────────────────────────────────────────────────────────────────────
# 5. The structural guard
# ─────────────────────────────────────────────────────────────────────────────
SHARED_API = "HUBSPOT_API_EXCEPTIONS"
SHARED_ALL = "HUBSPOT_SDK_READ_ERRORS"
SHARED_TRANSPORT = "HUBSPOT_TRANSPORT_EXCEPTIONS"
_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        inner = _dotted(node.func)
        parts.append((inner or "?") + "()")
    else:
        return None
    return ".".join(reversed(parts))


def _parents(tree) -> dict:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _functions_up(node, parents) -> list:
    """Enclosing function definitions, innermost first."""
    out, cur = [], parents.get(node)
    while cur is not None:
        if isinstance(cur, _FUNC):
            out.append(cur)
        cur = parents.get(cur)
    return out


class _Module:
    """What the guard needs to know about one source file."""

    def __init__(self, source: str, shared_packages: set | None = None):
        self.tree = ast.parse(source)
        self.parents = _parents(self.tree)
        self.functions = {n.name: n for n in self.tree.body
                          if isinstance(n, _FUNC)}
        # Local name -> package, for `from hubspot.crm.<pkg>[.exceptions]
        # import XException [as Y]`.
        self.class_pkg: dict = {}
        # Local name -> package, for `import hubspot.crm.<pkg> as m` and
        # `from hubspot.crm import <pkg>`.
        self.module_pkg: dict = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module.split(".")
                if mod[:2] == ["hubspot", "crm"] and len(mod) >= 3:
                    for a in node.names:
                        if a.name.endswith("Exception"):
                            self.class_pkg[a.asname or a.name] = mod[2]
                elif mod == ["hubspot", "crm"]:
                    for a in node.names:
                        self.module_pkg[a.asname or a.name] = a.name
            elif isinstance(node, ast.Import):
                for a in node.names:
                    mod = a.name.split(".")
                    if mod[:2] == ["hubspot", "crm"] and len(mod) >= 3 and a.asname:
                        self.module_pkg[a.asname] = mod[2]
        # The packages HUBSPOT_API_EXCEPTIONS really covers: read from its
        # definition, so a call into a package missing from the tuple (e.g.
        # associations) is not "caught" by naming the tuple.
        self.shared_packages = set(shared_packages or ())
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == SHARED_API):
                self.shared_packages = {
                    self.class_pkg[n.id] for n in ast.walk(node.value)
                    if isinstance(n, ast.Name) and n.id in self.class_pkg}
        # `crm = client.crm`: names that stand for the `crm` namespace.
        self.crm_aliases = {
            t.id for node in ast.walk(self.tree) if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
            and isinstance(node.value, ast.Attribute) and node.value.attr == "crm"}

    def package_of(self, node) -> str | None:
        """``deals`` for ``client.crm.deals.search_api`` / ``crm.deals…``."""
        inner = node
        while isinstance(inner, ast.Attribute):
            value = inner.value
            if isinstance(value, ast.Attribute) and value.attr == "crm":
                return inner.attr
            if isinstance(value, ast.Name) and value.id in self.crm_aliases:
                return inner.attr
            inner = value
        return None

    def handler_kinds(self, handler) -> set:
        """What one ``except`` clause catches, as tokens:
        ``pkg:<name>``, ``shared``, ``transport``, ``broad``, ``requests``."""
        if handler.type is None:
            return {"broad"}
        kinds = set()
        for node in ast.walk(handler.type):
            if isinstance(node, ast.Name):
                name = node.id
                if name in self.class_pkg:
                    kinds.add(f"pkg:{self.class_pkg[name]}")
                elif name == SHARED_API:
                    kinds.add("shared")
                elif name == SHARED_ALL:
                    kinds |= {"shared", "transport"}
                elif name == SHARED_TRANSPORT:
                    kinds.add("transport")
                elif name in ("Exception", "BaseException"):
                    kinds.add("broad")
            elif isinstance(node, ast.Attribute) and not isinstance(
                    self.parents.get(node), ast.Attribute):
                dotted = _dotted(node) or ""
                parts = dotted.split(".")
                if parts[:2] == ["hubspot", "crm"] and len(parts) >= 4:
                    kinds.add(f"pkg:{parts[2]}")
                elif parts[0] in self.module_pkg and parts[-1].endswith("Exception"):
                    kinds.add(f"pkg:{self.module_pkg[parts[0]]}")
                elif parts[0] == "urllib3":
                    kinds.add("transport")
                elif "RequestException" in dotted:
                    kinds.add("requests")
        return kinds

    def tries_around(self, node) -> list:
        """Handler kinds of each try whose BODY holds ``node``, within the
        innermost function (a lambda belongs to its defining function)."""
        out = []
        child, cur = node, self.parents.get(node)
        while cur is not None and not isinstance(cur, _FUNC):
            if isinstance(cur, ast.Try) and child in cur.body:
                out.append(set().union(*(self.handler_kinds(h)
                                         for h in cur.handlers)))
            child, cur = cur, self.parents.get(cur)
        return out

    def helper_of(self, node):
        """A module function the expression is handed to, directly or inside
        a lambda: ``_bounded_search(lambda: client.crm…)``."""
        cur = self.parents.get(node)
        while cur is not None and not isinstance(cur, _FUNC):
            if (isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name)
                    and cur.func.id in self.functions):
                return self.functions[cur.func.id]
            cur = self.parents.get(cur)
        return None

    def helper_kinds(self, helper) -> set:
        kinds = set()
        for node in ast.walk(helper):
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    kinds |= self.handler_kinds(h)
        return kinds

    def guards(self, node, depth: int = 0) -> list:
        """Every handler set that would see an error raised at ``node``."""
        found = list(self.tries_around(node))
        helper = self.helper_of(node)
        if helper is not None:
            found.append(self.helper_kinds(helper))
        if found or depth > 3:
            return found
        # A nested def with no handler of its own: judged at its call sites
        # inside the enclosing function.
        funcs = _functions_up(node, self.parents)
        if len(funcs) >= 2:
            inner, outer = funcs[0], funcs[1]
            for call in ast.walk(outer):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == inner.name):
                    found.extend(self.guards(call, depth + 1))
        return found


def sdk_call_sites(source: str, shared_packages: set | None = None) -> list:
    """Every ``client.crm.<package>`` use in ``source``, with the handlers that
    guard it and the violations found there.

    Recognised shapes: the plain chain; ``crm = client.crm`` then
    ``crm.<pkg>…``; a local alias ``api = client.crm.<pkg>.search_api`` (each
    USE of ``api`` is a site); a call inside a lambda handed to a module
    helper; a nested def with no handler, judged where it is called;
    ``except`` naming a class by import, by module alias or by full dotted
    path; ``HUBSPOT_API_EXCEPTIONS`` counted only for the packages its
    definition lists.
    """
    if shared_packages is None and source is not None:
        shared_packages = _connector_shared_packages()
    mod = _Module(source, shared_packages)
    sites = []

    def _record(node, pkg):
        funcs = _functions_up(node, mod.parents)
        site = {"line": node.lineno, "package": pkg,
                "function": funcs[-1].name if funcs else "<module>",
                "expr": _dotted(node), "guards": [], "violations": []}
        for kinds in mod.guards(node):
            site["guards"].append(sorted(kinds))
            pkg_kinds = {k for k in kinds if k.startswith("pkg:")}
            names_some_sdk_class = bool(pkg_kinds or "shared" in kinds)
            covers = (f"pkg:{pkg}" in kinds
                      or ("shared" in kinds and pkg in mod.shared_packages))
            if names_some_sdk_class and not covers:
                site["violations"].append(
                    f"line {node.lineno}: a client.crm.{pkg} call is guarded "
                    f"by {sorted(kinds)}, which does not catch the {pkg} "
                    "package's ApiException")
            if "requests" in kinds and not ({"transport", "broad"} & kinds):
                site["violations"].append(
                    f"line {node.lineno}: a client.crm.{pkg} call is guarded "
                    "for transport failures by requests.RequestException, "
                    "which the SDK never raises; its transport errors are "
                    "urllib3 errors (HUBSPOT_SDK_READ_ERRORS)")
        sites.append(site)

    aliases: dict = {}     # (function node, name) -> package
    for node in ast.walk(mod.tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            pkg = mod.package_of(node.value)
            if pkg is not None:
                funcs = _functions_up(node, mod.parents)
                aliases[(funcs[0] if funcs else None,
                         node.targets[0].id)] = pkg

    for node in ast.walk(mod.tree):
        if isinstance(node, ast.Attribute):
            pkg = mod.package_of(node)
            if pkg is None or isinstance(mod.parents.get(node), ast.Attribute):
                continue            # one site per chain, at its outermost end
            parent = mod.parents.get(node)
            if isinstance(parent, ast.Assign) and parent.value is node \
                    and isinstance(parent.targets[0], ast.Name):
                continue            # an alias: its USES are the sites
            _record(node, pkg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            funcs = _functions_up(node, mod.parents)
            for scope in ([funcs[0]] if funcs else []) + [None]:
                pkg = aliases.get((scope, node.id))
                if pkg is not None:
                    _record(node, pkg)
                    break
    return sites


@functools.lru_cache(maxsize=1)
def _connector_shared_packages() -> frozenset:
    return frozenset(_Module(CONNECTOR.read_text()).shared_packages)


def test_5_a_every_except_around_an_sdk_call_names_that_packages_class_or_the_shared_tuple():
    sites = sdk_call_sites(CONNECTOR.read_text())
    violations = [v for s in sites for v in s["violations"]]
    assert violations == [], "\n".join(violations)


def test_5_b_the_guard_sees_every_package_and_both_known_defect_sites():
    assert _connector_shared_packages() == {"contacts", "deals", "companies"}
    sites = sdk_call_sites(CONNECTOR.read_text())
    packages = {s["package"] for s in sites}
    assert {"contacts", "deals", "companies"} <= packages
    functions = {s["function"] for s in sites}
    assert {"pull_deals_for_ledger", "pull_contact_attribution_properties",
            "iter_contacts_modified_since", "_iter_closed_won_deal_pages",
            "read_company_names"} <= functions
    # Not vacuous: the 18 uses audited in PR-ADS-166B, at least.
    assert len(sites) >= 18, len(sites)
    # Every one of them is guarded by something, or propagates by design.
    unguarded = sorted(s["function"] for s in sites if not s["guards"])
    assert unguarded == ["pull_contacts_by_ids"], unguarded


def test_5_c_the_guard_flags_a_deals_call_caught_with_the_contacts_class():
    bad = (
        "from hubspot.crm.contacts import ApiException\n"
        "def pull(client):\n"
        "    try:\n"
        "        client.crm.deals.search_api.do_search()\n"
        "    except ApiException:\n"
        "        pass\n")
    (site,) = sdk_call_sites(bad)
    assert site["package"] == "deals"
    assert site["violations"], "the guard accepted the PR-ADS-166B defect"


def test_5_d_the_guard_flags_a_requests_only_transport_catch_around_an_sdk_call():
    bad = (
        "import requests\n"
        "from hubspot.crm.contacts import ApiException\n"
        "def pull(client):\n"
        "    try:\n"
        "        client.crm.contacts.batch_api.read()\n"
        "    except (ApiException, requests.exceptions.RequestException):\n"
        "        pass\n")
    (site,) = sdk_call_sites(bad)
    assert site["violations"]


_HEADER = ("import hubspot.crm.contacts as contacts_mod\n"
           "from hubspot.crm.contacts import ApiException\n"
           "from hubspot.crm.deals import ApiException as DealsApiException\n"
           "HUBSPOT_API_EXCEPTIONS = (ApiException, DealsApiException)\n")

BLIND_SPOTS = {
    "local alias of an API object": (
        "def pull(client):\n"
        "    api = client.crm.deals.search_api\n"
        "    try:\n"
        "        api.do_search()\n"
        "    except ApiException:\n"
        "        pass\n"),
    "crm = client.crm": (
        "def pull(client):\n"
        "    crm = client.crm\n"
        "    try:\n"
        "        crm.deals.search_api.do_search()\n"
        "    except ApiException:\n"
        "        pass\n"),
    "nested def called inside the try": (
        "def pull(client):\n"
        "    def page():\n"
        "        return client.crm.deals.search_api.do_search()\n"
        "    try:\n"
        "        page()\n"
        "    except ApiException:\n"
        "        pass\n"),
    "module-alias except": (
        "def pull(client):\n"
        "    try:\n"
        "        client.crm.deals.search_api.do_search()\n"
        "    except contacts_mod.ApiException:\n"
        "        pass\n"),
    "full dotted except": (
        "import hubspot\n"
        "def pull(client):\n"
        "    try:\n"
        "        client.crm.deals.search_api.do_search()\n"
        "    except hubspot.crm.contacts.exceptions.ApiException:\n"
        "        pass\n"),
    "associations under a tuple that lacks it": (
        "def pull(client):\n"
        "    try:\n"
        "        client.crm.associations.v4.basic_api.get_page()\n"
        "    except HUBSPOT_API_EXCEPTIONS:\n"
        "        pass\n"),
}


@pytest.mark.parametrize("shape", sorted(BLIND_SPOTS))
def test_5_f_the_guard_flags_each_shape_the_review_found_it_blind_to(shape):
    sites = sdk_call_sites(_HEADER + BLIND_SPOTS[shape])
    assert sites, f"{shape}: no SDK site found"
    assert any(s["violations"] for s in sites), f"{shape}: not flagged"


def test_5_g_the_same_shapes_pass_when_they_catch_the_right_class():
    good = (_HEADER
            + "def pull(client):\n"
              "    api = client.crm.deals.search_api\n"
              "    crm = client.crm\n"
              "    def page():\n"
              "        return crm.deals.search_api.do_search()\n"
              "    try:\n"
              "        api.do_search()\n"
              "        page()\n"
              "    except HUBSPOT_API_EXCEPTIONS:\n"
              "        pass\n")
    sites = sdk_call_sites(good)
    assert len(sites) == 2
    assert [v for s in sites for v in s["violations"]] == []


def test_5_e_no_production_module_other_than_the_connector_calls_the_sdk():
    """The audit's scope claim: ``client.crm.*`` lives in one module. A new
    caller elsewhere must be audited (and guarded) before this is widened."""
    offenders = []
    for folder in ("analysis", "api", "connectors", "db", "scheduler",
                   "services", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path == CONNECTOR:
                continue
            if sdk_call_sites(path.read_text()):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
