"""Capstone demo Streamlit UI for migration run, approval gate, and reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time
from typing import Any

import requests
import streamlit as st


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY = "capstone-api-key"
DEFAULT_REVIEWER_ID = "demo-reviewer"
DEFAULT_API_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3
TERMINAL_STATUSES = {"APPROVED", "REJECTED", "MODIFY_REQUESTED"}


@dataclass(frozen=True)
class ApiContext:
    base_url: str
    headers: dict[str, str]
    timeout_seconds: int


def _api_request(
    context: ApiContext,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    url = f"{context.base_url.rstrip('/')}{path}"
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=context.headers,
            json=payload,
            timeout=context.timeout_seconds,
        )
    except requests.RequestException as exc:
        return False, f"Request failed: {exc}"

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        return False, f"HTTP {response.status_code}: {detail}"

    try:
        return True, response.json()
    except ValueError:
        return False, "API returned a non-JSON response"


def _safe_load_report(path_str: str) -> str:
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return f"Report file not found: {path_str}"

    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Unable to read report {path_str}: {exc}"


def _build_api_context() -> ApiContext:
    st.sidebar.header("API Connection")
    base_url = st.sidebar.text_input("Base URL", value=DEFAULT_API_BASE_URL)
    timeout_seconds = st.sidebar.number_input(
        "Request timeout (seconds)",
        min_value=10,
        max_value=600,
        value=DEFAULT_API_TIMEOUT_SECONDS,
        step=10,
    )

    auth_mode = st.sidebar.selectbox("Auth Mode", options=["API Key", "Bearer Token"])
    headers: dict[str, str] = {}

    if auth_mode == "API Key":
        api_key = st.sidebar.text_input("x-api-key", value=DEFAULT_API_KEY, type="password")
        reviewer_id = st.sidebar.text_input("x-reviewer-id", value=DEFAULT_REVIEWER_ID)
        headers["x-api-key"] = api_key
        headers["x-reviewer-id"] = reviewer_id
    else:
        bearer_token = st.sidebar.text_input("Authorization Bearer Token", value="", type="password")
        reviewer_id = st.sidebar.text_input("x-reviewer-id", value=DEFAULT_REVIEWER_ID)
        headers["Authorization"] = f"Bearer {bearer_token.strip()}"
        headers["x-reviewer-id"] = reviewer_id

    return ApiContext(
        base_url=base_url.strip() or DEFAULT_API_BASE_URL,
        headers=headers,
        timeout_seconds=int(timeout_seconds),
    )


def _show_summary(summary: dict[str, Any]) -> None:
    st.subheader("Plan Summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total", summary.get("total_items", 0))
    col2.metric("Auto-Migratable", summary.get("auto_migratable_count", 0))
    col3.metric("Requires Review", summary.get("requires_review_count", 0))
    col4.metric("High Risk", summary.get("high_risk_count", 0))


def _status_section(context: ApiContext) -> None:
    st.subheader("Migration Run")

    source_reference = st.text_input("Source Reference", value="fixture://capstone-demo")
    bicep_path = st.text_input(
        "Bicep Path",
        value="tests/fixtures/bicep/functionapp_sample.bicep",
        help="Path to a .bicep file or directory to discover and parse for real.",
    )
    use_live_azure = st.checkbox("Use live Azure discovery", value=False)
    subscription_id = st.text_input("Subscription ID (optional)", value="")
    resource_group = st.text_input("Resource Group (optional)", value="")

    if st.button("Start Migration Run", use_container_width=True):
        payload: dict[str, Any] = {
            "source_reference": source_reference,
            "bicep_path": bicep_path,
            "use_live_azure": use_live_azure,
        }
        if subscription_id.strip():
            payload["subscription_id"] = subscription_id.strip()
        if resource_group.strip():
            payload["resource_group"] = resource_group.strip()

        ok, data = _api_request(
            context,
            "POST",
            "/migrations",
            payload,
        )
        if ok:
            st.session_state["migration_id"] = data["migration_id"]
            st.session_state["plan_hash"] = data["plan_hash"]
            st.success(f"Created migration run: {data['migration_id']}")
        else:
            st.error(str(data))

    migration_id = st.text_input(
        "Migration ID",
        value=st.session_state.get("migration_id", ""),
        placeholder="mig-...",
    ).strip()

    if migration_id:
        st.session_state["migration_id"] = migration_id

    poll_enabled = st.checkbox("Auto-poll status", value=False)
    poll_now = st.button("Poll Status Now", use_container_width=True)

    if migration_id and poll_now:
        ok, status_data = _api_request(context, "GET", f"/migrations/{migration_id}/status")
        if ok:
            st.session_state["status_payload"] = status_data
            st.session_state["plan_hash"] = status_data.get("plan_hash", "")
        else:
            st.error(str(status_data))

    status_payload = st.session_state.get("status_payload")
    if migration_id and status_payload and status_payload.get("migration_id") == migration_id:
        st.write(
            {
                "migration_id": status_payload.get("migration_id"),
                "status": status_payload.get("status"),
                "plan_hash": status_payload.get("plan_hash"),
                "updated_at": status_payload.get("updated_at"),
                "has_approval_record": status_payload.get("has_approval_record"),
            }
        )
        _show_summary(status_payload.get("summary", {}))

        current_status = status_payload.get("status")
        if poll_enabled and current_status not in TERMINAL_STATUSES:
            time.sleep(POLL_INTERVAL_SECONDS)
            st.rerun()


def _plan_section(context: ApiContext) -> None:
    st.subheader("Plan Details")

    migration_id = st.session_state.get("migration_id", "").strip()
    if not migration_id:
        st.info("Start a migration run or provide migration ID to view the plan.")
        return

    if st.button("Load Plan", use_container_width=True):
        ok, plan_data = _api_request(context, "GET", f"/migrations/{migration_id}/plan")
        if ok:
            st.session_state["plan_payload"] = plan_data
        else:
            st.error(str(plan_data))

    plan_payload = st.session_state.get("plan_payload")
    if not plan_payload or plan_payload.get("migration_id") != migration_id:
        return

    _show_summary(plan_payload.get("summary", {}))

    items = plan_payload.get("items", [])
    if items:
        st.dataframe(items, use_container_width=True)

        st.markdown("Per-item detail")
        for item in items:
            title = f"{item.get('resource_id', 'resource')} ({item.get('status', 'UNKNOWN')})"
            with st.expander(title):
                st.write(
                    {
                        "action": item.get("action"),
                        "risk_score": item.get("risk_score"),
                        "rule_id": item.get("rule_id"),
                    }
                )
                notes = item.get("notes", [])
                if notes:
                    st.markdown("Risk rationale / notes")
                    for note in notes:
                        st.write(f"- {note}")

    with st.expander("Planner Markdown Summary", expanded=False):
        st.markdown(plan_payload.get("markdown_summary", ""))


def _approval_section(context: ApiContext) -> None:
    st.subheader("Approval Gate")

    migration_id = st.session_state.get("migration_id", "").strip()
    plan_hash = st.session_state.get("plan_hash", "").strip()

    if not migration_id:
        st.info("Load a migration run first to submit approval actions.")
        return

    decision = st.selectbox("Decision", options=["APPROVE", "REJECT", "MODIFY"])
    rationale = st.text_area(
        "Rationale",
        placeholder="Required for REJECT and MODIFY. Optional for APPROVE.",
    )

    if st.button("Submit Decision", use_container_width=True):
        payload: dict[str, Any] = {
            "decision": decision,
            "plan_hash": plan_hash,
        }
        if rationale.strip():
            payload["rationale"] = rationale.strip()

        ok, approval_data = _api_request(
            context,
            "POST",
            f"/migrations/{migration_id}/approval",
            payload,
        )
        if ok:
            st.session_state["approval_payload"] = approval_data
            st.success(f"Decision recorded: {approval_data.get('status')}")
        else:
            st.error(str(approval_data))

    approval_payload = st.session_state.get("approval_payload")
    if approval_payload and approval_payload.get("migration_id") == migration_id:
        st.write(approval_payload)


def _reports_section(context: ApiContext) -> None:
    st.subheader("Final Reports")

    migration_id = st.session_state.get("migration_id", "").strip()
    if not migration_id:
        st.info("Load a migration run first to view reports.")
        return

    if st.button("Load Reports", use_container_width=True):
        ok, reports_data = _api_request(context, "GET", f"/migrations/{migration_id}/reports")
        if ok:
            st.session_state["reports_payload"] = reports_data
        else:
            st.error(str(reports_data))

    reports_payload = st.session_state.get("reports_payload")
    if not reports_payload or reports_payload.get("migration_id") != migration_id:
        return

    report_paths = reports_payload.get("report_paths", [])
    st.write(
        {
            "migration_id": reports_payload.get("migration_id"),
            "plan_hash": reports_payload.get("plan_hash"),
            "has_approval_record": reports_payload.get("has_approval_record"),
            "generated_at": reports_payload.get("generated_at"),
        }
    )

    if not report_paths:
        st.info("No reports are currently available for this run.")
        return

    st.markdown("Report artifacts")
    for path in report_paths:
        st.write(f"- {path}")

    for path in report_paths:
        if path.endswith(".md"):
            with st.expander(path, expanded=False):
                st.markdown(_safe_load_report(path))
        elif path.endswith(".json"):
            with st.expander(path, expanded=False):
                payload = _safe_load_report(path)
                try:
                    st.json(json.loads(payload))
                except json.JSONDecodeError:
                    st.text(payload)


def main() -> None:
    st.set_page_config(page_title="Migration Capstone UI", layout="wide")
    st.title("Azure-to-AWS Migration Capstone Demo")
    st.caption("Single-page demo for run kickoff, planning, approval gate, and report review.")

    context = _build_api_context()

    _status_section(context)
    st.divider()
    _plan_section(context)
    st.divider()
    _approval_section(context)
    st.divider()
    _reports_section(context)


if __name__ == "__main__":
    main()
