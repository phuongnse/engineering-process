import unittest

from verification.validate_publish_event import (
    PublishEventError,
    validate_publish_event,
)


def event() -> dict[str, object]:
    return {
        "action": "engineering-process-release-ready",
        "client_payload": {
            "attestationDigest": f"sha256:{'a' * 64}",
            "commit": "b" * 40,
            "repository": "phuongnse/engineering-process",
            "tag": "v0.2.0",
            "version": "0.2.0",
        },
        "repository": {"full_name": "phuongnse/engineering-process"},
        "sender": {"login": "phuongnse-renovate-ops[bot]"},
    }


class PublishEventTests(unittest.TestCase):
    def test_exact_app_authored_event_is_accepted(self):
        self.assertEqual(event()["client_payload"], validate_publish_event(event()))

    def test_different_sender_is_rejected(self):
        candidate = event()
        candidate["sender"]["login"] = "phuongnse"

        with self.assertRaisesRegex(PublishEventError, "release GitHub App"):
            validate_publish_event(candidate)

    def test_extra_payload_authority_is_rejected(self):
        candidate = event()
        candidate["client_payload"]["skipExisting"] = True

        with self.assertRaisesRegex(PublishEventError, "unexpected fields"):
            validate_publish_event(candidate)

    def test_tag_commit_and_digest_are_bound(self):
        cases = (
            ("tag", "v0.2.1", "tag does not match"),
            ("commit", "pending", "invalid commit"),
            ("attestationDigest", "sha256:pending", "attestation digest"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                candidate = event()
                candidate["client_payload"][field] = value
                with self.assertRaisesRegex(PublishEventError, message):
                    validate_publish_event(candidate)


if __name__ == "__main__":
    unittest.main()
