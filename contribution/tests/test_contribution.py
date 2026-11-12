# Create your tests here.
from django.test import TestCase

from contribution.test_helpers import create_test_premium
from core.test_helpers import create_test_officer
from insuree.test_helpers import create_test_insuree
from payment.test_helpers import create_test_payment2
from policy.models import Policy
from policy.test_helpers import create_test_policy2
from product.test_helpers import create_test_product, create_test_product_service


class TaskGroupServiceTest(TestCase):
    def test_helper(self):
        officer = create_test_officer(custom_props={"code": "TSTSIMP1"})
        insuree = create_test_insuree(custom_props={"chf_id": "paysimp"})
        product = create_test_product("ELI1")
        (policy, insuree_policy) = create_test_policy2(product, insuree, custom_props={
            "value": 1000, "status": Policy.STATUS_IDLE})
        premium = create_test_premium(policy_id=policy.id, with_payer=False)
