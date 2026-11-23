"""PR-ADS-171D — the ad-set backfill on a live account: contract checks.

The production scenarios run on a real schema in
test_pr_ads_171d_meta_adset_backfill_pg_integration.py. These cases need no
database: the shipped lead type, the attribution summary, and the structure
that keeps the campaign re-read inside the backfill and out of the ad-set
service and the daily ad-set step.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import scheduler.incremental_sync as sync
from services import meta_ads_adset_service as adsets
from services import meta_ads_adset_truth_service as adset_truth
from services import meta_ads_spend_service as meta

ROOT = Path(__file__).resolve().parent.parent


def test_01_the_shipped_lead_type_is_the_native_form_counter_and_one_type():
    assert adsets.native_lead_action_type() == "onsite_conversion.lead_grouped"
    # A type the sync stores (it keeps types containing "lead"), never a list.
    assert adset_truth.lead_type_stored("onsite_conversion.lead_grouped")
    raw = (ROOT / "config/thresholds.yaml").read_text()
    assert re.search(r'^\s*native_lead_action_type:\s*"onsite_conversion\.lead_grouped"\s*$',
                     raw, re.M)
    # Published only where observed: absent from an account's rows is not 0.
    row = {"rows_without_actions": 0,
           "meta_reported_lead_action_counts": {"lead": 4}}
    leads = adset_truth.lead_metrics(row, adsets.native_lead_action_type(),
                                     type_observed=False)["meta_reported_leads"]
    assert leads["value"] is None
    assert leads["reason"] == "meta_lead_action_type_not_observed"
    # Five identical overlapping types: the configured one's count, not 5x.
    five = {t: 7 for t in ("lead", "onsite_conversion.lead_grouped",
                           "offsite_complete_registration_add_meta_leads",
                           "offsite_content_view_add_meta_leads",
                           "offsite_search_add_meta_leads")}
    figures = adset_truth.lead_metrics(
        {"rows_without_actions": 0, "meta_reported_lead_action_counts": five},
        adsets.native_lead_action_type(), type_observed=True)
    assert figures["meta_reported_leads"]["value"] == 7


def test_02_the_attribution_setting_is_read_from_stored_rows_never_assumed():
    summary = adset_truth.attribution_summary
    assert summary([]) == {"settings": [], "rows_without_setting": 0,
                           "status": "no_rows", "detail": None}
    stored = [{"attribution_settings": ["1d_view_7d_click"],
               "attribution_setting_missing_rows": 0}]
    assert summary(stored)["status"] == "stored"
    assert summary(stored)["settings"] == ["1d_view_7d_click"]
    partly = stored + [{"attribution_settings": [],
                        "attribution_setting_missing_rows": 2}]
    assert summary(partly)["status"] == "partly_stored"
    assert summary(partly)["rows_without_setting"] == 2
    none = [{"attribution_settings": [], "attribution_setting_missing_rows": 3}]
    out = summary(none)
    assert out["status"] == "not_stored" and out["settings"] == []
    assert out["detail"].startswith("not stored")
    # No literal setting is written into the read path: it comes from rows.
    source = inspect.getsource(adset_truth)
    assert "1d_view_7d_click" not in source and "7d_click_1d_view" not in source


def test_03_the_campaign_re_read_lives_in_the_backfill_and_takes_no_override():
    chunk = inspect.getsource(meta._backfill_adset_chunk)
    calls = re.findall(r"\bsync_chunk\(([^)]*)\)", chunk)
    assert calls == ["account, start, end"], calls
    assert "accept_zero_restatement" not in calls[0]
    # Re-read first, ad sets after, and only on a verified re-read.
    assert chunk.index("sync_chunk(") < chunk.index("sync_adset_chunk(")
    assert 'coverage_status") != "verified"' in chunk
    backfill = inspect.getsource(meta._backfill_account)
    assert "_backfill_adset_chunk(" in backfill
    assert "sync_adset_chunk(" not in backfill
    # The ad-set service and the daily ad-set step still cannot write a
    # campaign table (test_28 of 171C holds the full list).
    service = (ROOT / "services/meta_ads_adset_service.py").read_text()
    for name in ("sync_chunk(", "replace_meta_campaign_spend_chunk",
                 "refresh_account", "record_meta_spend_coverage_failure"):
        assert name not in service, name
    step = inspect.getsource(sync._sync_meta_ads_adsets)
    assert "sync_chunk(" not in step and "_backfill_adset_chunk" not in step


def test_04_the_owner_never_needs_to_and_the_docs_say_what_still_fails():
    doc = (ROOT / "docs/53_META_ADS_ADSET_DETAIL.md").read_text()
    steps = doc[doc.index("## 10. Owner steps"):doc.index("## 11.")]
    commands = re.findall(r"python -m scripts\.backfill_meta_ads_spend[^\n]*", steps)
    assert commands and all("--to" not in c for c in commands), commands
    assert "stopped_at" in steps
    for evidence in ("onsite_conversion.lead_grouped",
                     "offsite_search_add_meta_leads", "1d_view_7d_click",
                     "2026-11-23"):
        assert evidence in doc, evidence
    script = (ROOT / "scripts/backfill_meta_ads_spend.py").read_text()
    assert "Run the campaign level (the" not in script
    assert "stopped_at" in script
    state = (ROOT / "docs/09_REPO_STATE.md").read_text()
    assert "PR-ADS-171D" in state
