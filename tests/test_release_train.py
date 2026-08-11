import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARDENED_SHA = "08f7d22f3a5b59b1658ab2e96a20d0d3c352869c"
RETIRED_SHA = "c981b872ebf650805200ad72c8b7142232f8b3f6"
EXPECTED_WORKFLOW = f"""name: Release train

# Announce-only Tool Drop train, shared across the whole hov catalog.
# All logic lives in hov-marketplace (the catalog announces itself);
# identity is the workflow's OIDC token — no shared secret, no per-repo
# scoping. Bump the pin when the reusable workflow changes.

on:
  release:
    types: [published, edited]

permissions:
  contents: read
  id-token: write

jobs:
  announce:
    uses: StartupBros-com/hov-marketplace/.github/workflows/hov-tool-drop-announce.yml@{HARDENED_SHA} # fix: bind Tool Drop intent to the promoted release
"""


def validate_release_train(workflow: str) -> None:
    if workflow != EXPECTED_WORKFLOW:
        raise ValueError("release train must exactly match the hardened Tool Drop policy")


class TestReleaseTrainPolicy(unittest.TestCase):
    def test_checked_in_workflow_is_canonical(self):
        workflow = (REPO_ROOT / ".github/workflows/release-train.yml").read_text()
        validate_release_train(workflow)

    def test_retired_pin_is_rejected(self):
        retired = EXPECTED_WORKFLOW.replace(HARDENED_SHA, RETIRED_SHA)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_release_train(retired)

    def test_decoy_pin_cannot_hide_wrong_announce_target(self):
        decoy = EXPECTED_WORKFLOW.replace(
            "jobs:\n  announce:\n    uses: StartupBros-com/",
            f"jobs:\n  decoy:\n    # blessed pin @{HARDENED_SHA}\n"
            "    uses: StartupBros-com/hov-marketplace/.github/workflows/"
            f"hov-tool-drop-announce.yml@{HARDENED_SHA}\n"
            "  announce:\n    uses: attacker/",
        )
        self.assertIn(HARDENED_SHA, decoy)
        self.assertIn("jobs:\n  decoy:", decoy)
        self.assertIn("\n  announce:\n    uses: attacker/", decoy)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_release_train(decoy)


if __name__ == "__main__":
    unittest.main()
