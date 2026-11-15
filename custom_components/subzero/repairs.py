"""Repairs that say what to do about problems that last."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


@callback
def async_report_firebase_alerts(
    hass: HomeAssistant, entry: ConfigEntry, problem: str | None
) -> None:
    """Raise a repair for Firebase alerts that keep failing, or clear it with None."""
    issue_id = f"firebase_alerts_{entry.entry_id}"
    if problem is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="firebase_alerts_failing",
        translation_placeholders={"problem": problem},
    )
