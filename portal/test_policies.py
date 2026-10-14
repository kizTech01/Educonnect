from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import AuditLog, GlobalPolicy, PolicyAcceptance, User


POLICY_SLUGS = (
    "privacy", "terms", "refunds", "acceptable-use", "cookies", "dpa", "security", "sla",
)


class PublishedPolicyTests(TestCase):
    def test_all_eight_policies_have_public_readable_versions_and_metadata(self):
        policies = GlobalPolicy.objects.filter(status=GlobalPolicy.Status.PUBLISHED)
        self.assertEqual(set(policies.values_list("slug", flat=True)), set(POLICY_SLUGS))
        for policy in policies:
            self.assertTrue(policy.title)
            self.assertTrue(policy.version)
            self.assertIsNotNone(policy.effective_date)
            self.assertIsNotNone(policy.last_updated)
            self.assertGreaterEqual(len(policy.sections), 3)
            self.assertTrue(all(item["heading"] and item["paragraphs"] for item in policy.sections))

            response = self.client.get(reverse("portal:policy-detail", args=[policy.slug]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, policy.title)
            self.assertContains(response, "Version")

    def test_landing_page_and_footer_link_to_every_policy(self):
        response = self.client.get(reverse("portal:policy-landing"))
        self.assertEqual(response.status_code, 200)
        for slug in POLICY_SLUGS:
            self.assertContains(response, reverse("portal:policy-detail", args=[slug]))

    def test_reading_a_policy_never_creates_acceptance_evidence(self):
        user = User.objects.create_user(
            username="policy-reader", password="pass12345", role=User.Role.STUDENT,
        )
        self.client.force_login(user)
        self.client.get(reverse("portal:policy-detail", args=["privacy"]))
        self.assertFalse(PolicyAcceptance.objects.filter(user=user).exists())


class PolicyAdministrationTests(TestCase):
    def setUp(self):
        self.super_admin = User.objects.create_superuser(
            username="policy-super-admin", email="legal@example.test", password="pass12345",
        )
        self.regular_user = User.objects.create_user(
            username="policy-student", password="pass12345", role=User.Role.STUDENT,
        )

    def test_non_super_admin_cannot_manage_global_policies(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("portal:super-admin-policies"))
        self.assertEqual(response.status_code, 403)

    def test_publishing_a_replacement_archives_old_version_and_audits_change(self):
        previous = GlobalPolicy.objects.get(slug="terms", status=GlobalPolicy.Status.PUBLISHED)
        replacement = GlobalPolicy.objects.create(
            slug="terms",
            title=previous.title,
            summary=previous.summary,
            sections=previous.sections,
            version="1.1-draft",
            effective_date=date(2026, 11, 1),
            last_updated=date(2026, 10, 10),
            acceptance_required=True,
            status=GlobalPolicy.Status.DRAFT,
            supersedes=previous,
        )
        self.client.force_login(self.super_admin)
        response = self.client.post(reverse("portal:super-admin-policy-publish", args=[replacement.pk]))
        self.assertRedirects(response, reverse("portal:super-admin-policies"))

        previous.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(previous.status, GlobalPolicy.Status.ARCHIVED)
        self.assertEqual(replacement.status, GlobalPolicy.Status.PUBLISHED)
        self.assertTrue(AuditLog.objects.filter(action="global_policy_published", object_id=str(replacement.pk)).exists())

        replacement.title = "Changed after publication"
        with self.assertRaises(ValidationError):
            replacement.save()

        public_response = self.client.get(reverse("portal:policy-detail", args=["terms"]))
        self.assertContains(public_response, "1.1-draft")
        self.assertNotContains(public_response, previous.version)
