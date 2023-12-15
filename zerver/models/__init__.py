from typing import Dict, List, Tuple, TypeVar, Union

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import CASCADE
from django.db.models.signals import post_delete, post_save
from django.db.models.sql.compiler import SQLCompiler
from django_stubs_ext import ValuesQuerySet
from typing_extensions import override

from zerver.lib.cache import (
    cache_delete,
    realm_alert_words_automaton_cache_key,
    realm_alert_words_cache_key,
)
from zerver.models.clients import Client as Client
from zerver.models.custom_profile_fields import CustomProfileField as CustomProfileField
from zerver.models.custom_profile_fields import CustomProfileFieldValue as CustomProfileFieldValue
from zerver.models.drafts import Draft as Draft
from zerver.models.groups import GroupGroupMembership as GroupGroupMembership
from zerver.models.groups import UserGroup as UserGroup
from zerver.models.groups import UserGroupMembership as UserGroupMembership
from zerver.models.linkifiers import RealmFilter as RealmFilter
from zerver.models.messages import AbstractAttachment as AbstractAttachment
from zerver.models.messages import AbstractEmoji as AbstractEmoji
from zerver.models.messages import AbstractMessage as AbstractMessage
from zerver.models.messages import AbstractReaction as AbstractReaction
from zerver.models.messages import AbstractSubMessage as AbstractSubMessage
from zerver.models.messages import AbstractUserMessage as AbstractUserMessage
from zerver.models.messages import ArchivedAttachment as ArchivedAttachment
from zerver.models.messages import ArchivedMessage as ArchivedMessage
from zerver.models.messages import ArchivedReaction as ArchivedReaction
from zerver.models.messages import ArchivedSubMessage as ArchivedSubMessage
from zerver.models.messages import ArchivedUserMessage as ArchivedUserMessage
from zerver.models.messages import ArchiveTransaction as ArchiveTransaction
from zerver.models.messages import Attachment as Attachment
from zerver.models.messages import Message as Message
from zerver.models.messages import Reaction as Reaction
from zerver.models.messages import SubMessage as SubMessage
from zerver.models.messages import UserMessage as UserMessage
from zerver.models.muted_users import MutedUser as MutedUser
from zerver.models.onboarding_steps import OnboardingStep as OnboardingStep
from zerver.models.prereg_users import EmailChangeStatus as EmailChangeStatus
from zerver.models.prereg_users import MultiuseInvite as MultiuseInvite
from zerver.models.prereg_users import PreregistrationRealm as PreregistrationRealm
from zerver.models.prereg_users import PreregistrationUser as PreregistrationUser
from zerver.models.prereg_users import RealmReactivationStatus as RealmReactivationStatus
from zerver.models.presence import UserPresence as UserPresence
from zerver.models.presence import UserStatus as UserStatus
from zerver.models.push_notifications import AbstractPushDeviceToken as AbstractPushDeviceToken
from zerver.models.push_notifications import PushDeviceToken as PushDeviceToken
from zerver.models.realm_audit_logs import AbstractRealmAuditLog as AbstractRealmAuditLog
from zerver.models.realm_audit_logs import RealmAuditLog as RealmAuditLog
from zerver.models.realm_emoji import RealmEmoji as RealmEmoji
from zerver.models.realm_playgrounds import RealmPlayground as RealmPlayground
from zerver.models.realms import Realm as Realm
from zerver.models.realms import RealmAuthenticationMethod as RealmAuthenticationMethod
from zerver.models.realms import RealmDomain as RealmDomain
from zerver.models.recipients import Huddle as Huddle
from zerver.models.recipients import Recipient as Recipient
from zerver.models.scheduled_jobs import AbstractScheduledJob as AbstractScheduledJob
from zerver.models.scheduled_jobs import MissedMessageEmailAddress as MissedMessageEmailAddress
from zerver.models.scheduled_jobs import ScheduledEmail as ScheduledEmail
from zerver.models.scheduled_jobs import ScheduledMessage as ScheduledMessage
from zerver.models.scheduled_jobs import (
    ScheduledMessageNotificationEmail as ScheduledMessageNotificationEmail,
)
from zerver.models.streams import DefaultStream as DefaultStream
from zerver.models.streams import DefaultStreamGroup as DefaultStreamGroup
from zerver.models.streams import Stream as Stream
from zerver.models.streams import Subscription as Subscription
from zerver.models.user_activity import UserActivity as UserActivity
from zerver.models.user_activity import UserActivityInterval as UserActivityInterval
from zerver.models.user_topics import UserTopic as UserTopic
from zerver.models.users import RealmUserDefault as RealmUserDefault
from zerver.models.users import UserBaseSettings as UserBaseSettings
from zerver.models.users import UserProfile as UserProfile


@models.Field.register_lookup
class AndZero(models.Lookup[int]):
    lookup_name = "andz"

    @override
    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> Tuple[str, List[Union[str, int]]]:  # nocoverage # currently only used in migrations
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} & {rhs} = 0", lhs_params + rhs_params


@models.Field.register_lookup
class AndNonZero(models.Lookup[int]):
    lookup_name = "andnz"

    @override
    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> Tuple[str, List[Union[str, int]]]:  # nocoverage # currently only used in migrations
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} & {rhs} != 0", lhs_params + rhs_params


ModelT = TypeVar("ModelT", bound=models.Model)
RowT = TypeVar("RowT")


def query_for_ids(
    query: ValuesQuerySet[ModelT, RowT],
    user_ids: List[int],
    field: str,
) -> ValuesQuerySet[ModelT, RowT]:
    """
    This function optimizes searches of the form
    `user_profile_id in (1, 2, 3, 4)` by quickly
    building the where clauses.  Profiling shows significant
    speedups over the normal Django-based approach.

    Use this very carefully!  Also, the caller should
    guard against empty lists of user_ids.
    """
    assert user_ids
    clause = f"{field} IN %s"
    query = query.extra(
        where=[clause],
        params=(tuple(user_ids),),
    )
    return query


# Interfaces for services
# They provide additional functionality like parsing message to obtain query URL, data to be sent to URL,
# and parsing the response.
GENERIC_INTERFACE = "GenericService"
SLACK_INTERFACE = "SlackOutgoingWebhookService"


# A Service corresponds to either an outgoing webhook bot or an embedded bot.
# The type of Service is determined by the bot_type field of the referenced
# UserProfile.
#
# If the Service is an outgoing webhook bot:
# - name is any human-readable identifier for the Service
# - base_url is the address of the third-party site
# - token is used for authentication with the third-party site
#
# If the Service is an embedded bot:
# - name is the canonical name for the type of bot (e.g. 'xkcd' for an instance
#   of the xkcd bot); multiple embedded bots can have the same name, but all
#   embedded bots with the same name will run the same code
# - base_url and token are currently unused
class Service(models.Model):
    name = models.CharField(max_length=UserProfile.MAX_NAME_LENGTH)
    # Bot user corresponding to the Service.  The bot_type of this user
    # determines the type of service.  If non-bot services are added later,
    # user_profile can also represent the owner of the Service.
    user_profile = models.ForeignKey(UserProfile, on_delete=CASCADE)
    base_url = models.TextField()
    token = models.TextField()
    # Interface / API version of the service.
    interface = models.PositiveSmallIntegerField(default=1)

    # Valid interfaces are {generic, zulip_bot_service, slack}
    GENERIC = 1
    SLACK = 2

    ALLOWED_INTERFACE_TYPES = [
        GENERIC,
        SLACK,
    ]
    # N.B. If we used Django's choice=... we would get this for free (kinda)
    _interfaces: Dict[int, str] = {
        GENERIC: GENERIC_INTERFACE,
        SLACK: SLACK_INTERFACE,
    }

    def interface_name(self) -> str:
        # Raises KeyError if invalid
        return self._interfaces[self.interface]


def get_bot_services(user_profile_id: int) -> List[Service]:
    return list(Service.objects.filter(user_profile_id=user_profile_id))


def get_service_profile(user_profile_id: int, service_name: str) -> Service:
    return Service.objects.get(user_profile_id=user_profile_id, name=service_name)


class BotStorageData(models.Model):
    bot_profile = models.ForeignKey(UserProfile, on_delete=CASCADE)
    key = models.TextField(db_index=True)
    value = models.TextField()

    class Meta:
        unique_together = ("bot_profile", "key")


class BotConfigData(models.Model):
    bot_profile = models.ForeignKey(UserProfile, on_delete=CASCADE)
    key = models.TextField(db_index=True)
    value = models.TextField()

    class Meta:
        unique_together = ("bot_profile", "key")


class AlertWord(models.Model):
    # Realm isn't necessary, but it's a nice denormalization.  Users
    # never move to another realm, so it's static, and having Realm
    # here optimizes the main query on this table, which is fetching
    # all the alert words in a realm.
    realm = models.ForeignKey(Realm, db_index=True, on_delete=CASCADE)
    user_profile = models.ForeignKey(UserProfile, on_delete=CASCADE)
    # Case-insensitive name for the alert word.
    word = models.TextField()

    class Meta:
        unique_together = ("user_profile", "word")


def flush_realm_alert_words(realm_id: int) -> None:
    cache_delete(realm_alert_words_cache_key(realm_id))
    cache_delete(realm_alert_words_automaton_cache_key(realm_id))


def flush_alert_word(*, instance: AlertWord, **kwargs: object) -> None:
    realm_id = instance.realm_id
    flush_realm_alert_words(realm_id)


post_save.connect(flush_alert_word, sender=AlertWord)
post_delete.connect(flush_alert_word, sender=AlertWord)