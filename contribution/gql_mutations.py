from contribution.services import premium_updated
from policy.models import Policy
from typing import Optional

import graphene
from contribution.apps import ContributionConfig
from contribution.models import Premium, PremiumMutation
from payer.models import Payer
from policy import models as policy_models
from core.schema import OpenIMISMutation
from core.models import Audits, Validity
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _
from core import datetime
from policy.services import PolicyService
from .services import update_or_create_premium, update_premium, create_premium
from insuree.uba import can_query, has_enrolment_perms, family_village
import logging

logger = logging.getLogger(__name__)


class PremiumBase:
    """
    This takes most parameters of the Premium with addition of action. This fields allows to force
    """
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    policy_uuid = graphene.String(required=True)
    payer_uuid = graphene.String()
    amount = graphene.Decimal()
    receipt = graphene.String()
    pay_date = graphene.Date()
    pay_type = graphene.String(max_length=1)
    is_offline = graphene.Boolean(required=False)
    is_photo_fee = graphene.Boolean(required=False)
    action = graphene.String(required=False)
    # json_ext = graphene.types.json.JSONString(required=False)


def reset_premium_before_update(premium):
    premium.amount = None
    premium.receipt = None
    premium.policy = None
    premium.payer = None
    premium.pay_date = None
    premium.pay_type = None
    premium.is_photo_fee = None
    premium.is_offline = None
    premium.reporting_id = None


def policy_village(policy):
    """A premium sits where the family of its policy does."""
    return family_village(policy.family) if policy is not None else None


def scoped_premium(user, premium_uuid):
    """The current premium `premium_uuid` if `user` may see it (its policy in scope)."""
    if not premium_uuid:
        return None
    return Premium.get_queryset(Premium.objects.filter(uuid=premium_uuid), user).first()


def premium_action(data, user, perms=None, update=None):
    """
    Create (`update` False) or update (`update` True) the premium of `data` for `user`;
    `update` None decides on whether `data['uuid']` names an existing premium the user
    may see. The policy, the premium updated and the payer are looked up within the
    user's scope, and `perms` (the create / update right, the create one by default) is
    checked on the policy's village (globally, or in the UBA bag with an ENROLMENT link).
    """
    if "client_mutation_id" in data:
        data.pop('client_mutation_id')
    if "client_mutation_label" in data:
        data.pop('client_mutation_label')
    # the row is designated by its uuid only: an id from the client would select any
    # premium (graphene does not enforce read_only)
    data.pop('id', None)
    perms = perms if perms is not None else ContributionConfig.gql_mutation_create_premiums_perms
    now = datetime.datetime.now()
    data['audit_user_id'] = user.id_for_audit
    data['validity_from'] = now
    policy_uuid = data.pop("policy_uuid") if "policy_uuid" in data else None
    if not policy_uuid:
        raise Exception(_("policy_uuid_required"))
    policy = Policy.get_queryset(Policy.objects.filter(uuid=policy_uuid), user).first()
    if not policy:
        raise Exception(_("policy_uuid_not_found") % (policy_uuid,))
    existing = scoped_premium(user, data.get('uuid'))
    if update is None:
        update = existing is not None
    if update:
        if existing is None:
            raise Exception(_("contribution.validation.id_does_not_exist") % {'id': data.get('uuid')})
        villages = [policy_village(existing.policy), policy_village(policy)]
    else:
        if data.get('uuid') and Premium.objects.filter(uuid=data['uuid']).exists():
            raise ValidationError(_("mutation.code_already_taken"))
        villages = [policy_village(policy)]
    if not has_enrolment_perms(user, perms, villages):
        raise PermissionDenied(_("unauthorized"))
    data["policy"] = policy

    # action: enforce, suspend, wait
    action = data.pop("action") if "action" in data else None
    payer_uuid = data.pop("payer_uuid") if "payer_uuid" in data else None
    payer = Payer.get_queryset(None, user).filter(uuid=payer_uuid).first() if payer_uuid else None
    if payer_uuid and payer is None:
        raise Exception(_("contribution.validation.payer_not_found") % {'id': payer_uuid})
    data["payer"] = payer
    premium = Premium(**data)
    # Handle the policy updating
    if update:
        return update_premium(existing, premium, user, action)
    return create_premium(premium, user, action)


class CreatePremiumMutation(OpenIMISMutation):
    """
    Create a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "CreatePremiumMutation"

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            perms = ContributionConfig.gql_mutation_create_premiums_perms
            if not can_query(user, perms):
                raise PermissionDenied(_("unauthorized"))
            client_mutation_id = data.get("client_mutation_id")
            premium = premium_action(data, user, perms=perms, update=False)
            PremiumMutation.object_mutated(user, client_mutation_id=client_mutation_id, premium=premium)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_create_premium"),
                'detail': str(exc)}
            ]


class UpdatePremiumMutation(OpenIMISMutation):
    """
    Update a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "UpdatePremiumMutation"

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            perms = ContributionConfig.gql_mutation_update_premiums_perms
            if not can_query(user, perms):
                raise PermissionDenied(_("unauthorized"))
            premium_action(data, user, perms=perms, update=True)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_update_premium") %
                {'id': data.get('uuid') if data else None},
                'detail': str(exc)}
            ]


class DeletePremiumsMutation(OpenIMISMutation):
    """
    Delete one or several Premiums.
    """
    _mutation_module = "contribution"
    _mutation_class = "DeletePremiumsMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        perms = ContributionConfig.gql_mutation_delete_premiums_perms
        if not can_query(user, perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for premium_uuid in data["uuids"]:
            # a premium out of the user's scope (or already deleted) does not exist for them
            premium = scoped_premium(user, premium_uuid)
            if premium is None:
                errors.append({
                    'title': premium_uuid,
                    'list': [{'message': _(
                        "contribution.validation.id_does_not_exist") % {'id': premium_uuid}}]
                })
                continue
            if not has_enrolment_perms(user, perms, [policy_village(premium.policy)]):
                errors.append({'title': premium_uuid, 'list': [{'message': _("unauthorized")}]})
                continue
            failure = set_premium_deleted(premium)
            if failure:
                errors.append(failure)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


def set_premium_deleted(premium):
    try:
        premium.delete_history()
        return []
    except Exception as exc:
        logger.debug("Exception when deleting premium %s", premium.uuid, exc_info=exc)
        return {
            'title': premium.uuid,
            'list': [{
                'message': _("contribution.mutation.failed_to_delete_premium") % {'uuid': premium.uuid},
                'detail': premium.uuid}]
        }


def on_policy_mutation(sender, **kwargs):
    """
    After a DeletePoliciesMutation (bound to the after-mutating signal: the policy
    mutation has checked its rights and scope): the current premiums of the policies it
    did delete are deleted with them. A policy it refused is still current and keeps
    its premiums.
    """
    errors = []
    if kwargs.get("mutation_class") == 'DeletePoliciesMutation':
        uuids = kwargs['data'].get('uuids', [])
        deleted = policy_models.Policy.objects.filter(uuid__in=uuids, validity_to__isnull=False)
        for premium in Premium.objects.filter(policy__in=deleted, validity_to__isnull=True):
            failure = set_premium_deleted(premium)
            if failure:
                errors.append(failure)
    return errors


def on_premium_mutation(sender, **kwargs):
    uuids = kwargs['data'].get('uuids', [])
    if not uuids:
        uuid = kwargs['data'].get('uuid', None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    # only the premiums the user may see get the mutation linked
    impacted_premiums = Premium.get_queryset(
        Premium.objects.filter(uuid__in=uuids), kwargs.get('user'), validity=Validity.ALL, audits=Audits.ALL)
    for premium in impacted_premiums:
        PremiumMutation.objects.update_or_create(premium=premium, mutation_id=kwargs['mutation_log_id'])
    return []
