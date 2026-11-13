"""
Security parity of contributions (premiums): rows and writes stay within the policies the
user may see (Premium.row_scope = ParentScope("policy"): district + ENROLMENT UBA link).

Fixture: core.test_helpers.create_security_tree -> R1>D1>W1>V1 / R2>D2>W2>V2; a policy
with a premium in each village; `user_a` holds the premium rights globally and is assigned
district D1 only.
"""
import datetime
import json

from django.core.cache import cache
from django.test import TestCase, override_settings

from contribution.apps import ContributionConfig
from contribution.models import Premium
from contribution.test_helpers import create_test_premium
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase
from core.test_helpers import create_security_tree, create_right_only_user, get_test_token, perm_ids
from insuree.test_helpers import create_test_insuree
from policy.apps import PolicyConfig
from policy.models import Policy
from policy.test_helpers import create_test_policy


def _u(*lists):
    return list(dict.fromkeys(r for lst in lists for r in lst))


def _product(code):
    """product.test_helpers imports program.test_helpers, absent on Cameroon's program."""
    from program.models import Program
    from product.models import Product
    program = Program.objects.create(code=code[:10], nameProgram=f"Program {code}")
    return Product.objects.create(
        code=code, name=f"Test product {code}", lump_sum=100, max_members=5,
        grace_period_enrolment=1, insurance_period=12, date_from="2019-01-01", date_to="2049-01-01",
        validity_from="2019-01-01", audit_user_id=-1, program=program)


PREMIUM_RIGHTS = _u(perm_ids(ContributionConfig.gql_query_premiums_perms),
                    perm_ids(ContributionConfig.gql_mutation_create_premiums_perms),
                    perm_ids(ContributionConfig.gql_mutation_update_premiums_perms),
                    perm_ids(ContributionConfig.gql_mutation_delete_premiums_perms))
POLICY_RIGHTS = _u(perm_ids(PolicyConfig.gql_query_policies_perms),
                   perm_ids(PolicyConfig.gql_mutation_delete_policies_perms))


def _build(cls, prefix):
    cls.t = create_security_tree(prefix)
    cls.product = _product(f"{prefix}PR")
    for key, village in (("a", cls.t.v1), ("b", cls.t.v2)):
        insuree = create_test_insuree(
            with_family=True, is_head=True,
            custom_props={"chf_id": f"{prefix}0000{1 if key == 'a' else 2}", "current_village": village},
            family_custom_props={"location": village})
        policy = create_test_policy(cls.product, insuree, link=True)
        Policy.objects.filter(id=policy.id).update(value=5000)  # not fully paid: no activation
        policy.refresh_from_db()
        premium = create_test_premium(policy.id, with_payer=False,
                                      custom_props={"receipt": f"{prefix}-{key}"})
        setattr(cls, f"pol_{key}", policy)
        setattr(cls, f"prem_{key}", premium)
    cls.user_a = create_right_only_user(f"{prefix.lower()}user", _u(PREMIUM_RIGHTS, POLICY_RIGHTS), [cls.t.d1.id])


def _messages(result):
    return json.dumps(result or [], default=str).lower()


@override_settings(ROW_SECURITY=True)
class PremiumReadScopeTest(openIMISGraphQLTestCase):

    @classmethod
    def setUpTestData(cls):
        _build(cls, "CTR")
        cls.token = get_test_token(cls.user_a)

    def setUp(self):
        cache.clear()

    def _q(self, query, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return json.loads(self.query(query, headers=headers).content)

    def test_model_scope(self):
        ids = set(Premium.get_queryset(Premium.objects.all(), self.user_a).values_list("id", flat=True))
        self.assertIn(self.prem_a.id, ids)
        self.assertNotIn(self.prem_b.id, ids)

    def test_premiums_listed_within_the_districts(self):
        content = self._q('{ premiums { edges { node { receipt } } } }', self.token)
        self.assertNotIn("errors", content, content.get("errors"))
        receipts = {e["node"]["receipt"] for e in content["data"]["premiums"]["edges"]}
        self.assertIn("CTR-a", receipts)
        self.assertNotIn("CTR-b", receipts)

    def test_premiums_by_policies_out_of_scope_left_out(self):
        content = self._q(
            f'{{ premiumsByPolicies(policyUuids: ["{self.pol_a.uuid}", "{self.pol_b.uuid}"]) '
            f'{{ edges {{ node {{ receipt }} }} }} }}', self.token)
        self.assertNotIn("errors", content, content.get("errors"))
        receipts = {e["node"]["receipt"] for e in content["data"]["premiumsByPolicies"]["edges"]}
        self.assertEqual({"CTR-a"}, receipts)

    def test_node_out_of_scope_not_found(self):
        from graphql_relay import to_global_id
        node_id = to_global_id("PremiumGQLType", self.prem_b.id)
        content = self._q(f'{{ node(id: "{node_id}") {{ ... on PremiumGQLType {{ receipt }} }} }}', self.token)
        self.assertIsNone((content.get("data") or {}).get("node"), content)
        anonymous = self._q(f'{{ node(id: "{node_id}") {{ ... on PremiumGQLType {{ receipt }} }} }}')
        self.assertIsNone((anonymous.get("data") or {}).get("node"), anonymous)

    def test_deleted_premiums_only_on_request(self):
        Premium.objects.filter(id=self.prem_a.id).update(validity_to=datetime.datetime(2020, 1, 1))
        content = self._q('{ premiums { edges { node { receipt } } } }', self.token)
        self.assertNotIn("CTR-a", {e["node"]["receipt"] for e in content["data"]["premiums"]["edges"]})
        history = self._q('{ premiums(showHistory: true) { edges { node { receipt } } } }', self.token)
        self.assertIn("CTR-a", {e["node"]["receipt"] for e in history["data"]["premiums"]["edges"]})

    def test_validate_code_on_a_policy_out_of_scope_refused(self):
        content = self._q(
            f'{{ validatePremiumCode(code: "X", policyUuid: "{self.pol_b.uuid}") }}', self.token)
        self.assertTrue(content.get("errors"), content)


@override_settings(ROW_SECURITY=True)
class PremiumWriteScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        _build(cls, "CTW")

    def setUp(self):
        cache.clear()

    def _create(self, policy, **extra):
        from contribution.gql_mutations import CreatePremiumMutation
        return CreatePremiumMutation.async_mutate(self.user_a, **{
            "policy_uuid": str(policy.uuid), "amount": 10, "receipt": f"NEW-{policy.id}",
            "pay_date": datetime.date(2024, 1, 1), "pay_type": "C", **extra})

    def test_create_on_a_policy_out_of_scope_refused(self):
        result = self._create(self.pol_b)
        self.assertTrue(result, "refused")
        self.assertFalse(Premium.objects.filter(receipt=f"NEW-{self.pol_b.id}").exists())

    def test_create_on_a_policy_in_scope_allowed(self):
        self.assertIsNone(self._create(self.pol_a))
        self.assertTrue(Premium.objects.filter(receipt=f"NEW-{self.pol_a.id}", policy=self.pol_a).exists())

    def test_create_with_the_id_of_another_premium_does_not_touch_it(self):
        """graphene does not enforce read_only: an id from the client is ignored"""
        self._create(self.pol_a, id=self.prem_b.id)
        self.assertEqual(self.pol_b.id, Premium.objects.get(id=self.prem_b.id).policy_id)

    def test_create_with_the_uuid_of_another_premium_refused(self):
        self._create(self.pol_a, uuid=self.prem_b.uuid)
        prem_b = Premium.objects.get(id=self.prem_b.id)
        self.assertEqual((self.pol_b.id, "CTW-b"), (prem_b.policy_id, prem_b.receipt))

    def test_update_out_of_scope_refused(self):
        from contribution.gql_mutations import UpdatePremiumMutation
        result = UpdatePremiumMutation.async_mutate(self.user_a, **{
            "uuid": self.prem_b.uuid, "policy_uuid": str(self.pol_a.uuid), "amount": 1,
            "receipt": "MOVED", "pay_date": datetime.date(2024, 1, 1), "pay_type": "C"})
        self.assertTrue(result)
        self.assertEqual("CTW-b", Premium.objects.get(id=self.prem_b.id).receipt)

    def test_delete_out_of_scope_does_not_exist(self):
        from contribution.gql_mutations import DeletePremiumsMutation
        result = DeletePremiumsMutation.async_mutate(self.user_a, uuids=[self.prem_b.uuid])
        self.assertIn("does_not_exist", _messages(result))
        self.assertIsNone(Premium.objects.get(id=self.prem_b.id).validity_to)

    def test_delete_in_scope_allowed(self):
        from contribution.gql_mutations import DeletePremiumsMutation
        self.assertEqual([], DeletePremiumsMutation.async_mutate(self.user_a, uuids=[self.prem_a.uuid]))
        self.assertIsNotNone(Premium.objects.get(id=self.prem_a.id).validity_to)

    def test_unknown_uuids_give_their_own_error(self):
        from contribution.gql_mutations import DeletePremiumsMutation
        unknown = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
        result = DeletePremiumsMutation.async_mutate(self.user_a, uuids=unknown)
        self.assertEqual(unknown, [error.get("title") for error in result])


@override_settings(ROW_SECURITY=True)
class DeletePoliciesCascadeTest(TestCase):
    """The premiums follow a policy deleted by DeletePoliciesMutation, after its checks."""

    @classmethod
    def setUpTestData(cls):
        _build(cls, "CTD")

    def _signal(self, uuids):
        from contribution.gql_mutations import on_policy_mutation
        return on_policy_mutation(None, mutation_class="DeletePoliciesMutation",
                                  data={"uuids": uuids}, user=self.user_a, error_messages=[])

    def test_bound_after_the_policy_mutation(self):
        import openIMIS.schema  # noqa: F401 -- builds the schema, which binds the modules' signals
        from core.schema import signal_mutation_module_before_mutating, signal_mutation_module_after_mutating
        from contribution.gql_mutations import on_policy_mutation
        before = [r[1]() for r in signal_mutation_module_before_mutating["policy"].receivers]
        after = [r[1]() for r in signal_mutation_module_after_mutating["policy"].receivers]
        self.assertNotIn(on_policy_mutation, before)
        self.assertIn(on_policy_mutation, after)

    def test_premiums_of_a_policy_not_deleted_untouched(self):
        """the policy mutation refused it (out of scope): the policy is current, so are its premiums"""
        self._signal([str(self.pol_b.uuid)])
        self.assertIsNone(Premium.objects.get(id=self.prem_b.id).validity_to)

    def test_premiums_follow_a_deleted_policy(self):
        Policy.objects.filter(id=self.pol_a.id).update(validity_to=datetime.datetime.now())
        self._signal([str(self.pol_a.uuid)])
        self.assertIsNotNone(Premium.objects.get(id=self.prem_a.id).validity_to)
