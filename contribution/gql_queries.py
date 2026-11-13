import graphene
from graphene_django import DjangoObjectType

from .apps import ContributionConfig
from .models import Premium, PremiumMutation
from core import prefix_filterset, ExtendedConnection
from core.gql.scoped_object_type import ScopedQuerysetMixin
from policy.schema import PolicyGQLType
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _


class PremiumGQLType(ScopedQuerysetMixin, DjangoObjectType):
    client_mutation_id = graphene.String()
    other_premiums = graphene.Float(source="other_premiums")

    class Meta:
        model = Premium
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "uuid": ["exact"],
            "amount": ["exact", "lt", "lte", "gt", "gte"],
            "pay_date": ["exact", "lt", "lte", "gt", "gte"],
            "pay_type": ["exact"],
            "is_photo_fee": ["exact"],
            "receipt": ["exact", "icontains"],
            **prefix_filterset("policy__", PolicyGQLType._meta.filter_fields)
        }
        connection_class = ExtendedConnection

    def resolve_client_mutation_id(self, info):
        if not info.context.user.has_perms(ContributionConfig.gql_query_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        premium_mutation = self.mutations.select_related(
            'mutation').filter(mutation__status=0).first()
        return premium_mutation.mutation.client_mutation_id if premium_mutation else None


class PremiumMutationGQLType(DjangoObjectType):
    class Meta:
        model = PremiumMutation
