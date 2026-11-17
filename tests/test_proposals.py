import pytest

from examples.triage import validated_step


@pytest.mark.parametrize(
    "proposal",
    [
        {"tool": "shell", "payload": {"ticket_id": "T01", "category": "billing"}},
        {"tool": "record", "payload": {"ticket_id": "T02", "category": "billing"}},
        {"tool": "record", "payload": {"ticket_id": "T01", "category": "delete"}},
        {
            "tool": "record",
            "payload": {
                "ticket_id": "T01",
                "category": "billing",
                "command": "delete everything",
            },
        },
        {
            "tool": "record",
            "payload": {"ticket_id": "T01", "category": "billing"},
            "url": "http://untrusted",
        },
    ],
)
def test_invalid_model_proposals_are_rejected_before_submission(proposal):
    with pytest.raises(ValueError):
        validated_step(proposal, "T01")


def test_valid_model_proposal_has_only_allowlisted_fields():
    proposal = {
        "tool": "record",
        "payload": {"ticket_id": "T01", "category": "billing"},
    }
    assert validated_step(proposal, "T01") == proposal
